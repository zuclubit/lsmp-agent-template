"""
Documentation Context Server — MCP Server for Migration Platform Documentation

Indexes all Markdown files under /docs, /architecture, /security/compliance
and provides keyword-based semantic search to agents.

MCP Resources:
  - docs://{path}                     — read a specific documentation file
  - docs://index                      — list all indexed documents

MCP Tools:
  - search_docs(query, max_results)   -> list[DocumentationChunk]
  - get_document(path)                -> DocumentationChunk
  - list_documents(directory)         -> list[DocumentationSummary]
  - search_runbook(operation)         -> list[DocumentationChunk]
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_PATH = os.environ.get(
    "DOC_CONTEXT_CONFIG",
    os.path.join(os.path.dirname(__file__), "config.json"),
)

REPO_ROOT = os.environ.get(
    "REPO_ROOT",
    "/Users/oscarvalois/Documents/Github/s-agent",
)

def _load_config() -> dict[str, Any]:
    try:
        with open(CONFIG_PATH) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}

CONFIG: dict[str, Any] = _load_config()

_INDEXED_DIRS: list[str] = CONFIG.get("indexed_directories", [
    os.path.join(REPO_ROOT, "docs"),
    os.path.join(REPO_ROOT, "architecture"),
    os.path.join(REPO_ROOT, "security", "compliance"),
    os.path.join(REPO_ROOT, "halcon"),
])

_CHUNK_SIZE_CHARS: int = CONFIG.get("chunk_size_chars", 1500)
_CHUNK_OVERLAP_CHARS: int = CONFIG.get("chunk_overlap_chars", 200)
_INDEX_TTL_SECONDS: int = CONFIG.get("index_ttl_seconds", 3600)

# ---------------------------------------------------------------------------
# Domain Types
# ---------------------------------------------------------------------------

@dataclass
class DocumentationChunk:
    chunk_id: str
    document_path: str
    document_title: str
    section_heading: Optional[str]
    content: str
    char_offset: int
    char_length: int
    keywords: list[str]
    relevance_score: float  # 0.0 - 1.0, set by search


@dataclass
class DocumentationSummary:
    path: str
    title: str
    size_bytes: int
    chunk_count: int
    keywords: list[str]
    last_modified: str


# ---------------------------------------------------------------------------
# Text Processing
# ---------------------------------------------------------------------------

def _extract_title(content: str, path: str) -> str:
    """Extract H1 heading from Markdown, fall back to filename."""
    match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return Path(path).stem.replace("-", " ").replace("_", " ").title()


def _extract_keywords(text: str) -> list[str]:
    """
    Extract meaningful keywords using TF-IDF-inspired term frequency.
    Stopwords are removed; terms are lowercased and deduplicated.
    """
    stopwords = {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
        "been", "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "could", "should", "may", "might", "must", "can", "that",
        "this", "these", "those", "it", "its", "if", "not", "no", "all",
        "each", "both", "few", "more", "most", "other", "into", "through",
        "during", "before", "after", "above", "below", "between", "out",
        "up", "down", "off", "over", "then", "once", "when", "where", "which",
        "who", "whom", "how", "what", "their", "they", "them", "we", "our",
        "your", "you", "he", "she", "his", "her", "my", "i", "me", "also",
    }
    words = re.findall(r"\b[a-zA-Z][a-zA-Z0-9_\-]{2,}\b", text.lower())
    freq: dict[str, int] = {}
    for w in words:
        if w not in stopwords:
            freq[w] = freq.get(w, 0) + 1

    # Return top 20 by frequency
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:20]]


def _extract_section_heading(text: str) -> Optional[str]:
    """Find the last Markdown heading before the chunk content."""
    match = re.search(r"^#{1,3}\s+(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _chunk_text(content: str, chunk_size: int = _CHUNK_SIZE_CHARS, overlap: int = _CHUNK_OVERLAP_CHARS) -> list[tuple[int, str]]:
    """
    Split text into overlapping chunks.
    Returns list of (char_offset, chunk_text) tuples.
    Prefers splitting at paragraph boundaries within the chunk window.
    """
    chunks: list[tuple[int, str]] = []
    start = 0
    length = len(content)

    while start < length:
        end = min(start + chunk_size, length)

        # Try to split at a paragraph boundary
        if end < length:
            boundary = content.rfind("\n\n", start, end)
            if boundary > start + chunk_size // 2:
                end = boundary + 2

        chunk = content[start:end]
        chunks.append((start, chunk))
        start = end - overlap if end < length else length

    return chunks


# ---------------------------------------------------------------------------
# Document Index
# ---------------------------------------------------------------------------

class DocumentIndex:
    """
    In-memory keyword index of all Markdown files in indexed directories.
    Rebuilt on startup and on TTL expiry.
    """

    def __init__(self) -> None:
        self._chunks: list[DocumentationChunk] = []
        self._summaries: dict[str, DocumentationSummary] = {}
        self._inverted_index: dict[str, list[int]] = {}  # keyword -> [chunk_idx]
        self._built_at: float = 0.0

    def is_stale(self) -> bool:
        return (time.monotonic() - self._built_at) > _INDEX_TTL_SECONDS

    def build(self) -> None:
        """Scan all configured directories and build the index."""
        logger.info("Building documentation index...")
        start = time.monotonic()
        self._chunks.clear()
        self._summaries.clear()
        self._inverted_index.clear()

        doc_files = self._discover_markdown_files()
        for path in doc_files:
            self._index_file(path)

        self._built_at = time.monotonic()
        elapsed = self._built_at - start
        logger.info(
            "Documentation index built: %d files, %d chunks in %.2fs",
            len(self._summaries),
            len(self._chunks),
            elapsed,
        )

    def _discover_markdown_files(self) -> list[str]:
        paths: list[str] = []
        for directory in _INDEXED_DIRS:
            if not os.path.isdir(directory):
                logger.debug("Indexed directory does not exist, skipping: %s", directory)
                continue
            for root, _, files in os.walk(directory):
                for fname in files:
                    if fname.endswith(".md"):
                        paths.append(os.path.join(root, fname))
        return sorted(paths)

    def _index_file(self, path: str) -> None:
        try:
            size = os.path.getsize(path)
            if size > 5_000_000:  # Skip files > 5MB
                logger.warning("Skipping oversized doc file: %s (%d bytes)", path, size)
                return

            with open(path, encoding="utf-8", errors="replace") as fh:
                content = fh.read()

            title = _extract_title(content, path)
            doc_keywords = _extract_keywords(content)
            mtime = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(path)))

            raw_chunks = _chunk_text(content)
            chunk_ids: list[str] = []

            for idx, (offset, chunk_text) in enumerate(raw_chunks):
                chunk_id = f"{path}#{idx}"
                keywords = _extract_keywords(chunk_text)
                section = _extract_section_heading(chunk_text)

                chunk = DocumentationChunk(
                    chunk_id=chunk_id,
                    document_path=path,
                    document_title=title,
                    section_heading=section,
                    content=chunk_text,
                    char_offset=offset,
                    char_length=len(chunk_text),
                    keywords=keywords,
                    relevance_score=0.0,
                )
                chunk_idx = len(self._chunks)
                self._chunks.append(chunk)
                chunk_ids.append(chunk_id)

                # Build inverted index
                for kw in keywords:
                    self._inverted_index.setdefault(kw, []).append(chunk_idx)

            self._summaries[path] = DocumentationSummary(
                path=path,
                title=title,
                size_bytes=size,
                chunk_count=len(raw_chunks),
                keywords=doc_keywords[:10],
                last_modified=mtime,
            )

        except Exception as exc:
            logger.error("Failed to index %s: %s", path, exc)

    def search(self, query: str, max_results: int = 5) -> list[DocumentationChunk]:
        """
        Keyword search using TF-IDF-inspired scoring.
        Each query term is looked up in the inverted index.
        Chunks are scored by: sum(term_frequency_in_chunk * idf_of_term)
        """
        if self.is_stale():
            self.build()

        query_terms = _extract_keywords(query)
        if not query_terms:
            # Fall back to simple substring search
            query_terms = [t.lower() for t in query.split() if len(t) > 2]

        n_chunks = len(self._chunks)
        if n_chunks == 0:
            return []

        scores: dict[int, float] = {}

        for term in query_terms:
            matching_chunk_idxs = self._inverted_index.get(term, [])
            if not matching_chunk_idxs:
                # Try prefix match
                matching_chunk_idxs = [
                    idx
                    for kw, idxs in self._inverted_index.items()
                    if kw.startswith(term)
                    for idx in idxs
                ]

            df = len(set(matching_chunk_idxs))  # document frequency
            idf = math.log((n_chunks + 1) / (df + 1)) + 1.0

            for chunk_idx in matching_chunk_idxs:
                chunk = self._chunks[chunk_idx]
                tf = chunk.keywords.count(term) + 1
                scores[chunk_idx] = scores.get(chunk_idx, 0.0) + (tf * idf)

        if not scores:
            return []

        max_score = max(scores.values())
        ranked = sorted(scores.items(), key=lambda x: -x[1])[:max_results]

        results: list[DocumentationChunk] = []
        for chunk_idx, score in ranked:
            chunk = self._chunks[chunk_idx]
            # Return a copy with relevance_score filled in
            scored_chunk = DocumentationChunk(
                chunk_id=chunk.chunk_id,
                document_path=chunk.document_path,
                document_title=chunk.document_title,
                section_heading=chunk.section_heading,
                content=chunk.content,
                char_offset=chunk.char_offset,
                char_length=chunk.char_length,
                keywords=chunk.keywords,
                relevance_score=round(score / max_score, 4) if max_score > 0 else 0.0,
            )
            results.append(scored_chunk)

        return results

    def get_document(self, path: str) -> list[DocumentationChunk]:
        """Return all chunks for a specific document path."""
        if self.is_stale():
            self.build()
        return [c for c in self._chunks if c.document_path == path]

    def list_summaries(self, directory: Optional[str] = None) -> list[DocumentationSummary]:
        if self.is_stale():
            self.build()
        summaries = list(self._summaries.values())
        if directory:
            summaries = [s for s in summaries if s.path.startswith(directory)]
        return sorted(summaries, key=lambda s: s.path)


# ---------------------------------------------------------------------------
# Documentation Context Server
# ---------------------------------------------------------------------------

_INDEX = DocumentIndex()


class DocumentationContextServer:
    """
    MCP-compatible context server providing documentation search and retrieval.
    """

    SERVER_ID = "documentation-context"
    SERVER_VERSION = "2.0.0"

    def handle_mcp_request(self, request: dict[str, Any]) -> dict[str, Any]:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        try:
            if method == "tools/call":
                tool_name = params["name"]
                arguments = params.get("arguments", {})
                result = self._dispatch_tool(tool_name, arguments)
                return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(result)}]}}

            elif method == "resources/read":
                uri = params["uri"]
                result = self._read_resource(uri)
                return {"jsonrpc": "2.0", "id": req_id, "result": {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": json.dumps(result)}]}}

            elif method == "tools/list":
                return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": _TOOL_DEFINITIONS}}

            elif method == "initialize":
                # Build index on initialization
                if _INDEX.is_stale():
                    _INDEX.build()
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": self.SERVER_ID, "version": self.SERVER_VERSION},
                        "capabilities": {"resources": {"subscribe": False}, "tools": {}},
                    },
                }

            else:
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}

        except ValueError as exc:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": str(exc)}}
        except Exception:
            logger.exception("Unhandled error in documentation context server")
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": "Internal error"}}

    def _dispatch_tool(self, tool_name: str, args: dict[str, Any]) -> Any:
        if tool_name == "search_docs":
            query = args.get("query", "")
            max_results = min(int(args.get("max_results", 5)), 20)
            if not query:
                raise ValueError("query is required")
            chunks = _INDEX.search(query, max_results)
            return {"results": [asdict(c) for c in chunks], "count": len(chunks)}

        elif tool_name == "get_document":
            path = args.get("path", "")
            if not path:
                raise ValueError("path is required")
            chunks = _INDEX.get_document(path)
            if not chunks:
                raise ValueError(f"Document not found in index: {path}")
            return {"path": path, "chunks": [asdict(c) for c in chunks]}

        elif tool_name == "list_documents":
            directory = args.get("directory")
            summaries = _INDEX.list_summaries(directory)
            return {"documents": [asdict(s) for s in summaries], "count": len(summaries)}

        elif tool_name == "search_runbook":
            operation = args.get("operation", "")
            if not operation:
                raise ValueError("operation is required")
            # Runbook search: bias toward runbook/procedure terminology
            query = f"{operation} runbook procedure steps how to"
            chunks = _INDEX.search(query, max_results=5)
            return {"operation": operation, "runbook_sections": [asdict(c) for c in chunks]}

        elif tool_name == "rebuild_index":
            _INDEX.build()
            return {"status": "rebuilt", "chunk_count": len(_INDEX._chunks), "document_count": len(_INDEX._summaries)}

        else:
            raise ValueError(f"Unknown tool: {tool_name}")

    def _read_resource(self, uri: str) -> Any:
        if uri == "docs://index":
            summaries = _INDEX.list_summaries()
            return {"documents": [asdict(s) for s in summaries]}
        elif uri.startswith("docs://"):
            path = uri[len("docs://"):]
            # Resolve to absolute path within repo
            if not path.startswith("/"):
                path = os.path.join(REPO_ROOT, path)
            chunks = _INDEX.get_document(path)
            if not chunks:
                raise ValueError(f"Document not indexed: {path}")
            full_content = "\n".join(c.content for c in chunks)
            return {"path": path, "content": full_content}
        else:
            raise ValueError(f"Unknown resource URI: {uri}")


# ---------------------------------------------------------------------------
# MCP Tool Definitions
# ---------------------------------------------------------------------------

_TOOL_DEFINITIONS = [
    {
        "name": "search_docs",
        "description": "Search indexed documentation using keyword matching",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "default": 5, "maximum": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_document",
        "description": "Retrieve all chunks of a specific documentation file",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "list_documents",
        "description": "List all indexed documentation files, optionally filtered by directory",
        "inputSchema": {
            "type": "object",
            "properties": {"directory": {"type": "string"}},
        },
    },
    {
        "name": "search_runbook",
        "description": "Search for runbook procedures for a given operation",
        "inputSchema": {
            "type": "object",
            "properties": {"operation": {"type": "string"}},
            "required": ["operation"],
        },
    },
    {
        "name": "rebuild_index",
        "description": "Force rebuild of the documentation index from disk",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    import sys

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    server = DocumentationContextServer()
    logger.info("DocumentationContextServer starting — reading from stdin")

    # Build index eagerly on startup
    _INDEX.build()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}}
            print(json.dumps(response), flush=True)
            continue

        response = server.handle_mcp_request(request)
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
