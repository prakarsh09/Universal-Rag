import os
import re
import sys
import glob
import time
import uuid
import shutil
import logging
import threading
import subprocess
from queue import Queue
import torch

# Enforce CUDA acceleration if available
if torch.cuda.is_available():
    os.environ["TORCH_DEVICE"] = "cuda"
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"
    os.environ["USE_GPU"] = "1"
from pathlib import Path
from typing import Optional, List, Dict, Any

# Ensure project root is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE_DIR, ".env"))

from utils import chunk_markdown

logger = logging.getLogger("document_worker")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("[DocumentWorker] %(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(ch)


class DocumentJob:
    def __init__(self, job_id: str, file_path: str, original_filename: str, document_name: str, category: str):
        self.job_id = job_id
        self.file_path = file_path
        self.original_filename = original_filename
        self.document_name = document_name
        self.category = category or "General"
        
        self.status = "queued"  # queued | converting | chunking | embedding | indexing | completed | failed
        self.progress_percent = 0
        self.status_message = "Waiting in queue..."
        self.chunks_count = 0
        self.error: Optional[str] = None
        
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.completed_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "original_filename": self.original_filename,
            "document_name": self.document_name,
            "category": self.category,
            "status": self.status,
            "progress_percent": self.progress_percent,
            "status_message": self.status_message,
            "chunks_count": self.chunks_count,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at
        }


class DocumentIngestionWorker:
    """Singleton background worker ensuring strictly 1 document is processed at a time.
    All CPU/DB-heavy operations run asynchronously in a dedicated native thread,
    guaranteeing ongoing chat prompts and WebSocket streams are never blocked.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, driver=None, embed_model=None):
        if self._initialized:
            if driver and not self.driver:
                self.driver = driver
            if embed_model and not self.embed_model:
                self.embed_model = embed_model
            return

        self.driver = driver
        self.embed_model = embed_model
        self.base_dir = BASE_DIR
        self.input_dir = os.path.join(self.base_dir, "input_docs")
        self.output_dir = os.path.join(self.base_dir, "markdown_output")
        self.image_dir = os.path.join(self.base_dir, "uploaded_images")

        os.makedirs(self.input_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.image_dir, exist_ok=True)

        self._queue: Queue = Queue()
        self._active_job: Optional[DocumentJob] = None
        self._history: List[Dict[str, Any]] = []
        self._state_lock = threading.Lock()

        # Start dedicated worker thread
        self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="DocIngestionThread")
        self._thread.start()
        self._initialized = True
        logger.info("Document Ingestion Worker initialized with concurrency limit = 1.")

    def _find_marker_executable(self) -> Optional[str]:
        """Locates marker_single executable in PATH or markervenv."""
        which_path = shutil.which("marker_single")
        if which_path:
            return which_path

        # Windows venv check
        venv_marker_win = os.path.join(self.base_dir, "markervenv", "Scripts", "marker_single.exe")
        if os.path.exists(venv_marker_win):
            return venv_marker_win

        # Linux/macOS venv check
        venv_marker_nix = os.path.join(self.base_dir, "markervenv", "bin", "marker_single")
        if os.path.exists(venv_marker_nix):
            return venv_marker_nix

        return None

    def enqueue(self, file_path: str, original_filename: str, category: str = "General") -> Dict[str, Any]:
        """Enqueues a document for processing. Concurrency is limited to 1; others wait in FIFO queue."""
        doc_stem = Path(original_filename).stem
        job_id = str(uuid.uuid4())
        job = DocumentJob(
            job_id=job_id,
            file_path=file_path,
            original_filename=original_filename,
            document_name=doc_stem,
            category=category or "General"
        )

        with self._state_lock:
            # Calculate queue position (1-indexed)
            queue_pos = self._queue.qsize() + 1
            self._queue.put(job)
            logger.info(f"Enqueued job {job_id} for '{original_filename}'. Queue position: {queue_pos}")

        job_dict = job.to_dict()
        job_dict["queue_position"] = queue_pos
        return job_dict

    def get_status(self) -> Dict[str, Any]:
        """Returns current worker state, active job, pending queue, and recent history."""
        cuda_ok = torch.cuda.is_available()
        device_name = torch.cuda.get_device_name(0) if cuda_ok else "CPU"
        with self._state_lock:
            active_data = self._active_job.to_dict() if self._active_job else None
            # Inspect items currently in queue without popping
            queued_items = []
            for idx, item in enumerate(list(self._queue.queue)):
                d = item.to_dict()
                d["queue_position"] = idx + 1
                queued_items.append(d)

            return {
                "is_busy": self._active_job is not None,
                "active_job": active_data,
                "queue": queued_items,
                "queue_count": len(queued_items),
                "history": list(reversed(self._history[-15:])),
                "device_info": {
                    "cuda_available": cuda_ok,
                    "device_name": device_name,
                    "device_type": "cuda" if cuda_ok else "cpu"
                }
            }

    def _worker_loop(self):
        """Worker thread loop - strictly processes one job at a time."""
        while True:
            job: DocumentJob = self._queue.get()
            try:
                with self._state_lock:
                    self._active_job = job
                    job.status = "converting"
                    job.started_at = time.time()
                    job.progress_percent = 10
                    job.status_message = "Starting document conversion..."

                logger.info(f"Started processing job {job.job_id} for '{job.original_filename}'")
                self._process_job(job)

                with self._state_lock:
                    job.status = "completed"
                    job.progress_percent = 100
                    job.completed_at = time.time()
                    job.status_message = f"Successfully indexed {job.chunks_count} chunks into graph."
                    self._history.append(job.to_dict())
                logger.info(f"Job {job.job_id} completed successfully ({job.chunks_count} chunks).")

            except Exception as e:
                logger.error(f"Error processing job {job.job_id}: {e}", exc_info=True)
                with self._state_lock:
                    job.status = "failed"
                    job.error = str(e)
                    job.completed_at = time.time()
                    job.status_message = f"Failed: {str(e)}"
                    self._history.append(job.to_dict())
            finally:
                with self._state_lock:
                    self._active_job = None
                self._queue.task_done()

    def _convert_pdf(self, job: DocumentJob, target_out_dir: Path, cuda_ok: bool, device_label: str) -> Path:
        """Converts a PDF to Markdown safely and resiliently.
        - Checks total page count and detect if document has digital text.
        - For large digital documents (>25 pages), uses high-speed streaming extraction
          via pdftext to prevent Marker's uncompressed bitmap MemoryError in pypdfium2.
        - For standard documents (<=25 pages), executes Marker layout analysis on CUDA.
        - Automatically falls back to high-fidelity pdftext extraction if Marker encounters MemoryError or fails.
        - For scanned large documents (>25 pages without digital text), runs Marker in 10-page chunks.
        """
        import pypdfium2
        from pdftext.extraction import paginated_plain_text_output

        pdf_path_str = str(job.file_path)
        md_file_path = target_out_dir / f"{job.document_name}.md"

        # 1. Total page count check
        page_count = 1
        try:
            pdf_doc = pypdfium2.PdfDocument(pdf_path_str)
            page_count = len(pdf_doc)
            pdf_doc.close()
        except Exception as e:
            logger.warning(f"Failed to read page count for {job.original_filename}: {e}")

        logger.info(f"Converting PDF '{job.original_filename}' ({page_count} pages) on {device_label}")
        job.status_message = f"Analyzing document structure ({page_count} pages) on {device_label}..."

        # 2. Check for extractable digital text
        is_digital = False
        try:
            sample_pages = paginated_plain_text_output(pdf_path_str, page_range=range(min(3, page_count)), sort=True)
            sample_len = sum(len(p.strip()) for p in sample_pages)
            is_digital = sample_len > 100
        except Exception as e:
            logger.warning(f"Could not sample PDF text: {e}")

        # 3. Large digital document (> 25 pages): Use high-speed streaming extractor directly
        # Marker allocates all high-res uncompressed page bitmaps at once, which throws MemoryError
        # on large PDFs (e.g. 402 pages = ~5.7 GB contiguous RAM).
        if page_count > 25 and is_digital:
            logger.info(f"PDF '{job.original_filename}' has {page_count} digital pages. Using high-speed memory-safe parser.")
            job.status_message = f"Parsing {page_count} pages via streaming layout engine..."
            pages = paginated_plain_text_output(pdf_path_str, sort=True)
            md_sections = []
            for idx, page in enumerate(pages, start=1):
                clean_page = page.strip()
                if clean_page:
                    md_sections.append(f"## Page {idx}\n\n{clean_page}")

            full_markdown = "\n\n".join(md_sections)
            with open(md_file_path, "w", encoding="utf-8") as f:
                f.write(full_markdown)
            logger.info(f"Extracted {page_count} pages ({len(full_markdown):,} chars) to '{md_file_path}'")
            return md_file_path

        # 4. Standard document (<= 25 pages) or Scanned document: Try Marker
        marker_exe = self._find_marker_executable()
        if marker_exe:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            if cuda_ok:
                env["TORCH_DEVICE"] = "cuda"
                env["CUDA_VISIBLE_DEVICES"] = "0"
                env["USE_GPU"] = "1"

            if page_count <= 25:
                # Run Marker directly on small document
                job.status_message = f"Running layout analysis on {device_label} ({page_count} pages)..."
                cmd = [
                    marker_exe,
                    pdf_path_str,
                    "--output_dir", str(target_out_dir),
                    "--output_format", "markdown",
                    "--disable_image_extraction"
                ]
                try:
                    res = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
                    if res.returncode == 0:
                        possible_mds = list(target_out_dir.glob("*.md")) + list(target_out_dir.rglob("*.md"))
                        if possible_mds and possible_mds[0].stat().st_size > 30:
                            return possible_mds[0]
                        else:
                            sz = possible_mds[0].stat().st_size if possible_mds else 0
                            logger.warning(f"Marker generated an empty or minimal markdown file ({sz} bytes). Proceeding to fallback.")
                    else:
                        logger.warning(f"Marker returned non-zero code ({res.returncode}): {res.stderr or res.stdout}")
                except Exception as e:
                    logger.warning(f"Marker execution failed: {e}")
            else:
                # Scanned large document: Chunked Marker conversion (10 pages per chunk) to prevent MemoryError
                logger.info(f"Scanned large PDF ({page_count} pages). Running chunked OCR in batches of 10 pages...")
                chunk_mds = []
                chunk_size = 10
                for start_p in range(0, page_count, chunk_size):
                    end_p = min(start_p + chunk_size - 1, page_count - 1)
                    page_range_str = f"{start_p}-{end_p}"
                    job.status_message = f"Running OCR on pages {start_p + 1}-{end_p + 1} of {page_count} on {device_label}..."
                    chunk_out_dir = target_out_dir / f"chunk_{start_p}_{end_p}"
                    os.makedirs(chunk_out_dir, exist_ok=True)
                    cmd = [
                        marker_exe,
                        pdf_path_str,
                        "--output_dir", str(chunk_out_dir),
                        "--output_format", "markdown",
                        "--page_range", page_range_str,
                        "--disable_image_extraction"
                    ]
                    try:
                        res = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
                        if res.returncode == 0:
                            chunk_files = list(chunk_out_dir.glob("*.md")) + list(chunk_out_dir.rglob("*.md"))
                            if chunk_files and chunk_files[0].stat().st_size > 20:
                                with open(chunk_files[0], "r", encoding="utf-8", errors="replace") as cf:
                                    chunk_mds.append(cf.read().strip())
                    except Exception as err:
                        logger.warning(f"Error on chunk {page_range_str}: {err}")

                if chunk_mds:
                    full_markdown = "\n\n".join(chunk_mds)
                    if len(full_markdown.strip()) > 30:
                        with open(md_file_path, "w", encoding="utf-8") as f:
                            f.write(full_markdown)
                        return md_file_path

        # 5. Fast text extraction fallback (pdftext)
        logger.info(f"Attempting digital text extraction for '{job.original_filename}' via pdftext...")
        try:
            pages = paginated_plain_text_output(pdf_path_str, sort=True)
            md_sections = []
            for idx, page in enumerate(pages, start=1):
                clean_page = page.strip()
                if clean_page:
                    md_sections.append(f"## Page {idx}\n\n{clean_page}")
            if md_sections:
                full_markdown = "\n\n".join(md_sections)
                if len(full_markdown.strip()) > 30:
                    with open(md_file_path, "w", encoding="utf-8") as f:
                        f.write(full_markdown)
                    logger.info(f"Fallback extraction successful: {len(full_markdown):,} chars written to '{md_file_path}'")
                    return md_file_path
        except Exception as fb_err:
            logger.warning(f"Fallback extraction via pdftext failed: {fb_err}")

        # 6. Multimodal Vision OCR Fallback (Gemini 2.5 Flash / Vertex AI)
        # For scanned PDFs, vector-outlined text, or image-only documents where OCR models failed to parse text
        vision_md = self._extract_via_vision_llm(job, target_out_dir, page_count)
        if vision_md and vision_md.exists() and vision_md.stat().st_size > 20:
            return vision_md

        raise RuntimeError(f"Could not convert '{job.original_filename}'. Marker, digital text, and Gemini Vision OCR could not extract any content.")

    def _extract_via_vision_llm(self, job: DocumentJob, target_out_dir: Path, page_count: int) -> Optional[Path]:
        """Transcribes scanned, image-only, or vector-outlined PDF pages using Gemini Multimodal Vision.
        Renders each page to a high-fidelity image in memory and generates structured markdown.
        """
        import pypdfium2
        from google import genai

        logger.info(f"Triggering Gemini Multimodal Vision OCR for '{job.original_filename}' ({page_count} pages)...")
        job.status_message = f"Transcribing scanned/image PDF with Gemini Multimodal Vision ({page_count} pages)..."

        project = os.getenv("VERTEX_PROJECT", "agent-506620")
        location = os.getenv("VERTEX_LOCATION", "global")
        model_name = os.getenv("LLM_MODEL_NAME", "gemini-2.5-flash")

        try:
            client = genai.Client(vertexai=True, project=project, location=location)
        except Exception as e:
            logger.error(f"Failed to initialize Vertex AI client for Vision OCR: {e}")
            return None

        md_file_path = target_out_dir / f"{job.document_name}.md"
        page_sections = []

        try:
            pdf_doc = pypdfium2.PdfDocument(str(job.file_path))
            for idx, page in enumerate(pdf_doc, start=1):
                job.status_message = f"Transcribing page {idx}/{page_count} with Gemini Vision OCR..."
                img = page.render(scale=2).to_pil()
                prompt = (
                    "Transcribe all text, tables, numbers, and structural details from this document page into clear, well-structured Markdown. "
                    "Preserve all headings, table structures (columns, rows, cell values), bullet points, and language text (English, Hindi, or any other language) exactly as shown. "
                    "Do not add conversational commentary or explanations; output only the Markdown content for this page."
                )
                response = client.models.generate_content(
                    model=model_name,
                    contents=[img, prompt]
                )
                txt = (response.text or "").strip()
                # Clean markdown code fences if wrapped
                if txt.startswith("```markdown"):
                    txt = txt[11:]
                elif txt.startswith("```"):
                    txt = txt[3:]
                if txt.endswith("```"):
                    txt = txt[:-3]
                txt = txt.strip()
                if txt:
                    page_sections.append(f"## Page {idx}\n\n{txt}")
            pdf_doc.close()

            if page_sections:
                full_md = "\n\n".join(page_sections)
                with open(md_file_path, "w", encoding="utf-8") as f:
                    f.write(full_md)
                logger.info(f"Gemini Vision OCR successfully extracted {page_count} pages ({len(full_md):,} chars) to '{md_file_path}'")
                return md_file_path
        except Exception as e:
            logger.error(f"Error during Gemini Vision OCR transcription: {e}", exc_info=True)

        return None

    def _process_job(self, job: DocumentJob):
        """Executes the complete document ingestion pipeline into Neo4j."""
        ext = Path(job.file_path).suffix.lower()
        target_out_dir = Path(self.output_dir) / job.category / job.document_name
        os.makedirs(target_out_dir, exist_ok=True)

        md_file_path = None

        # --- STEP 1: CONVERSION ---
        cuda_ok = torch.cuda.is_available()
        device_label = f"CUDA GPU ({torch.cuda.get_device_name(0)})" if cuda_ok else "CPU"

        if ext in [".jpeg", ".jpg", ".png", ".webp"]:
            return self._process_image_job(job, cuda_ok, device_label)

        if ext == ".pdf":
            job.status = "converting"
            job.progress_percent = 20
            md_file_path = self._convert_pdf(job, target_out_dir, cuda_ok, device_label)
        elif ext in [".md", ".markdown", ".txt"]:
            job.status = "converting"
            job.progress_percent = 20
            job.status_message = "Preparing markdown content..."
            md_file_path = target_out_dir / f"{job.document_name}.md"
            shutil.copyfile(job.file_path, md_file_path)
        else:
            raise ValueError(f"Unsupported document format '{ext}'. Supported: .pdf, .md, .txt")

        # Read markdown content
        with open(md_file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read().strip()

        if not content:
            raise ValueError(f"Extracted document text from '{job.original_filename}' is empty.")

        # --- STEP 2: CHUNKING & TABLE NARRATIVES ---
        job.status = "chunking"
        job.progress_percent = 40
        job.status_message = "Segmenting text into semantic chunks and generating tabular narratives..."

        raw_chunks = chunk_markdown(
            content,
            max_chunk_size=1000,
            doc_name=job.document_name,
            category=job.category
        )

        if not raw_chunks:
            raise ValueError("No text or table chunks could be parsed from the document.")

        doc_id = str(uuid.uuid4())
        processed_chunks = []
        for idx, chunk in enumerate(raw_chunks):
            chunk["category"] = job.category
            chunk["document_name"] = job.document_name
            chunk["doc_id"] = doc_id
            chunk["chunk_id"] = str(uuid.uuid4())
            chunk["chunk_index"] = idx
            processed_chunks.append(chunk)

        total_chunks = len(processed_chunks)
        job.chunks_count = total_chunks
        logger.info(f"Parsed {total_chunks} table-aware chunks for '{job.document_name}'.")

        # --- STEP 3: DENSE EMBEDDINGS ---
        job.status = "embedding"
        job.progress_percent = 60
        job.status_message = f"Computing dense embeddings on {device_label} for {total_chunks} chunks..."

        if not self.embed_model:
            raise RuntimeError("Embedding model is not configured.")

        # Ensure embedding model is on CUDA device
        if cuda_ok and hasattr(self.embed_model, "_model") and hasattr(self.embed_model._model, "to"):
            self.embed_model._model.to("cuda")

        batch_size = 32
        texts = [c["text"] for c in processed_chunks]
        all_embeddings = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            embeddings = self.embed_model.get_text_embedding_batch(batch)
            all_embeddings.extend(embeddings)
            embedded_so_far = min(i + batch_size, len(texts))
            job.progress_percent = 60 + int((embedded_so_far / len(texts)) * 20)
            job.status_message = f"Computing embeddings ({embedded_so_far}/{len(texts)} chunks)..."

        dim = len(all_embeddings[0]) if all_embeddings else 384

        # Attach embeddings to chunks
        for i, chunk in enumerate(processed_chunks):
            chunk["embedding"] = all_embeddings[i]

        # --- STEP 4: INCREMENTAL NEO4J INGESTION ---
        job.status = "indexing"
        job.progress_percent = 85
        job.status_message = "Writing chunks, relationships, and vector index to Neo4j..."

        if not self.driver:
            raise RuntimeError("Neo4j driver is not connected.")

        with self.driver.session() as session:
            # 1. Ensure Full-Text and Vector Indexes exist
            session.run("""
                CREATE FULLTEXT INDEX chunk_text_index IF NOT EXISTS
                FOR (n:Chunk)
                ON EACH [n.text]
            """)

            session.run(f"""
                CREATE VECTOR INDEX chunk_vector_index IF NOT EXISTS
                FOR (n:Chunk)
                ON (n.embedding)
                OPTIONS {{indexConfig: {{
                    `vector.dimensions`: {dim},
                    `vector.similarity_function`: 'cosine'
                }}}}
            """)

            # 2. Delete prior chunks for this document if re-uploading
            session.run("""
                MATCH (d:Document {name: $document_name})
                OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk)
                DETACH DELETE c, d
            """, {"document_name": job.document_name})

            # 3. Insert chunks in batches using UNWIND
            cypher_insert = """
                UNWIND $batch as chunk
                CREATE (c:Chunk {
                    id: chunk.chunk_id,
                    text: chunk.text,
                    chunk_type: chunk.type,
                    header_path: chunk.header_path,
                    category: chunk.category,
                    document_name: chunk.document_name,
                    doc_id: chunk.doc_id,
                    chunk_index: chunk.chunk_index,
                    embedding: chunk.embedding
                })
            """
            for i in range(0, len(processed_chunks), 50):
                sub_batch = processed_chunks[i:i + 50]
                session.run(cypher_insert, {"batch": sub_batch})

            # 4. Create Document node and link to Chunks
            session.run("""
                MERGE (d:Document {name: $document_name})
                ON CREATE SET d.id = $doc_id, d.category = $category
                ON MATCH SET d.id = $doc_id, d.category = $category
            """, {
                "document_name": job.document_name,
                "doc_id": doc_id,
                "category": job.category
            })

            session.run("""
                MATCH (c:Chunk {doc_id: $doc_id}), (d:Document {name: $document_name})
                MERGE (d)-[:HAS_CHUNK]->(c)
            """, {
                "doc_id": doc_id,
                "document_name": job.document_name
            })

            # 5. Link sequential chunks (NEXT) in O(N) linear time
            session.run("""
                MATCH (d:Document {name: $document_name})-[:HAS_CHUNK]->(c:Chunk)
                WITH c ORDER BY c.chunk_index ASC
                WITH collect(c) AS chunks
                WHERE size(chunks) > 1
                UNWIND range(0, size(chunks) - 2) AS idx
                WITH chunks[idx] AS c1, chunks[idx+1] AS c2
                MERGE (c1)-[:NEXT]->(c2)
            """, {"document_name": job.document_name})

            # 6. Category nodes & links
            session.run("""
                MATCH (d:Document {name: $document_name})
                WHERE d.category IS NOT NULL AND d.category <> 'General'
                MERGE (cat:Category {name: d.category})
                MERGE (d)-[:IN_CATEGORY]->(cat)
                MERGE (cat)-[:HAS_DOCUMENT]->(d)
            """, {"document_name": job.document_name})

        job.progress_percent = 100
        job.status_message = f"Indexing complete! {total_chunks} chunks stored in graph."

    def get_indexed_documents(self) -> List[Dict[str, Any]]:
        """Queries Neo4j for all indexed documents with categories and chunk counts."""
        if not self.driver:
            return []
            
        with self.driver.session() as session:
            result = session.run("""
                MATCH (d:Document)
                OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk)
                RETURN d.name as name, d.category as category, d.id as doc_id, count(c) as chunk_count
                ORDER BY d.name ASC
            """)
            docs = []
            for record in result:
                docs.append({
                    "name": record["name"],
                    "category": record["category"] or "General",
                    "doc_id": record["doc_id"] or "",
                    "chunk_count": record["chunk_count"]
                })
            return docs

    def delete_document(self, document_name: str) -> bool:
        """Removes a document and its associated chunks from the Neo4j Knowledge Graph."""
        if not self.driver:
            return False
            
        with self.driver.session() as session:
            session.run("""
                MATCH (d:Document {name: $document_name})
                OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk)
                DETACH DELETE c, d
            """, {"document_name": document_name})
            
            # Clean up empty Category nodes if any
            session.run("""
                MATCH (cat:Category)
                WHERE NOT (cat)-[:HAS_DOCUMENT]->()
                DETACH DELETE cat
            """)
            return True

    def _extract_ollama_vision_description(self, image_path: Path, category: str = "General") -> Dict[str, str]:
        """Calls local Ollama (gemma4:e2b) with base64 image to extract 3 key description points and a summary."""
        import base64
        import requests

        with open(image_path, "rb") as f:
            b64_img = base64.b64encode(f.read()).decode("utf-8")

        api_base = os.getenv("LLM_API_BASE", "http://localhost:11434/v1")
        host_port = api_base.replace("/v1", "").rstrip("/")
        generate_url = f"{host_port}/api/generate"
        model_name = os.getenv("LLM_MODEL_NAME", "gemma4:e2b")

        prompt = (
            "You are an expert visual AI. Analyze this image and provide:\n"
            "1. Exactly 3 concise, highly descriptive bullet points:\n"
            "   - Point 1 (Visual Subjects): Main entities, objects, people, diagrams, charts, scenes, or actions.\n"
            "   - Point 2 (Text & Data): Any readable text, labels, numbers, titles, metrics, or table contents.\n"
            "   - Point 3 (Context & Purpose): The functional context, domain, category, and key takeaway.\n"
            "2. A one-sentence summary.\n\n"
            "Output directly without conversational preamble. Format your output STRICTLY as follows:\n"
            "POINT 1: <description of visual content and entities>\n"
            "POINT 2: <visible text, titles, numbers, and labels>\n"
            "POINT 3: <context, domain, and purpose>\n"
            "SUMMARY: <1-line summary overview>"
        )

        payload = {
            "model": model_name,
            "prompt": prompt,
            "images": [b64_img],
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_predict": 1500
            }
        }

        raw_text = ""
        thinking_text = ""
        try:
            resp = requests.post(generate_url, json=payload, timeout=240)
            if resp.status_code == 200:
                resp_json = resp.json()
                raw_text = resp_json.get("response", "").strip()
                thinking_text = resp_json.get("thinking", "").strip()
            else:
                logger.error(f"Ollama vision request failed ({resp.status_code}): {resp.text}")
        except Exception as e:
            logger.error(f"Error calling Ollama vision: {e}")

        point1 = ""
        point2 = ""
        point3 = ""
        summary = ""

        def _parse_candidate_text(text: str):
            nonlocal point1, point2, point3, summary
            if not text:
                return
            cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

            p1_pat = r'(?:^|\n)\s*(?:\*\*|\*|#)*\s*(?:POINT\s*1|Point\s*1|1\.)[^\n:]*[:\-]?\s*(?:\*\*|\*)?\s*(.*?)(?=(?:\n\s*(?:\*\*|\*|#)*\s*(?:POINT\s*2|Point\s*2|2\.)|SUMMARY|Summary|\Z))'
            p2_pat = r'(?:^|\n)\s*(?:\*\*|\*|#)*\s*(?:POINT\s*2|Point\s*2|2\.)[^\n:]*[:\-]?\s*(?:\*\*|\*)?\s*(.*?)(?=(?:\n\s*(?:\*\*|\*|#)*\s*(?:POINT\s*3|Point\s*3|3\.)|SUMMARY|Summary|\Z))'
            p3_pat = r'(?:^|\n)\s*(?:\*\*|\*|#)*\s*(?:POINT\s*3|Point\s*3|3\.)[^\n:]*[:\-]?\s*(?:\*\*|\*)?\s*(.*?)(?=(?:\n\s*(?:\*\*|\*|#)*\s*(?:SUMMARY|Summary)|\Z))'
            sum_pat = r'(?:^|\n)\s*(?:\*\*|\*|#)*\s*(?:SUMMARY|Summary)[^\n:]*[:\-]?\s*(?:\*\*|\*)?\s*(.*)'

            m1 = re.search(p1_pat, cleaned, re.DOTALL | re.IGNORECASE)
            m2 = re.search(p2_pat, cleaned, re.DOTALL | re.IGNORECASE)
            m3 = re.search(p3_pat, cleaned, re.DOTALL | re.IGNORECASE)
            ms = re.search(sum_pat, cleaned, re.DOTALL | re.IGNORECASE)

            if m1 and not point1: point1 = m1.group(1).strip()
            if m2 and not point2: point2 = m2.group(1).strip()
            if m3 and not point3: point3 = m3.group(1).strip()
            if ms and not summary: summary = ms.group(1).strip()

            if not point1 or not point2 or not point3:
                clean_lines = [l.strip().lstrip('*-0123456789. ') for l in cleaned.split('\n') if l.strip()]
                clean_lines = [l for l in clean_lines if not l.lower().startswith("here are") and not l.lower().startswith("thinking") and not l.lower().startswith("analyze")]
                if len(clean_lines) >= 1 and not point1: point1 = clean_lines[0]
                if len(clean_lines) >= 2 and not point2: point2 = clean_lines[1]
                if len(clean_lines) >= 3 and not point3: point3 = clean_lines[2]
                if not summary and clean_lines: summary = clean_lines[0]

        _parse_candidate_text(raw_text)
        if (not point1 or not point2 or not point3) and thinking_text:
            _parse_candidate_text(thinking_text)

        if not point1 or not point2 or not point3:
            logger.warning(f"Ollama vision extraction returned empty or fallback points for {image_path.name}. Raw text: {raw_text[:200]}")

        if not point1: point1 = f"Visual image file {image_path.name}"
        if not point2: point2 = "No prominent text or annotations detected."
        if not point3: point3 = f"Image classified under {category}"
        if not summary: summary = f"{image_path.name} - {point1[:80]}"

        return {
            "point1": point1,
            "point2": point2,
            "point3": point3,
            "summary": summary,
            "raw_text": raw_text
        }

    def _process_image_job(self, job: DocumentJob, cuda_ok: bool, device_label: str):
        """Processes an uploaded image: detects and embeds faces via InsightFace ArcFace (512d),
        extracts 3 description points via Ollama vision, computes dense embedding,
        and creates (:Image) and (:Face) nodes with vector and full-text indexes.
        """
        # Step 1: Face detection & Vision analysis
        job.status = "converting"
        job.progress_percent = 20
        job.status_message = "Detecting faces (InsightFace) and analyzing visual content (gemma4:e2b)..."

        target_img_dir = Path(self.image_dir) / job.category
        os.makedirs(target_img_dir, exist_ok=True)
        dest_img_path = target_img_dir / job.original_filename

        if Path(job.file_path).resolve() != dest_img_path.resolve():
            shutil.copyfile(job.file_path, dest_img_path)

        image_url = f"/images/{job.category}/{job.original_filename}"

        # Detect and embed faces
        detected_faces = []
        try:
            from backend.face_engine import get_face_engine
            face_engine = get_face_engine()
            detected_faces = face_engine.detect_and_embed(dest_img_path)
            if detected_faces:
                faces_dir = Path(self.image_dir) / "_faces"
                face_engine.save_face_crops(dest_img_path, detected_faces, output_dir=faces_dir, file_prefix=dest_img_path.stem)
                logger.info(f"Detected {len(detected_faces)} face(s) in '{job.original_filename}'.")
        except Exception as e:
            logger.warning(f"Face detection failed on '{job.original_filename}': {e}")

        # Extract 3 visual points via local Ollama vision
        vision_data = self._extract_ollama_vision_description(dest_img_path, category=job.category)
        point1 = vision_data["point1"]
        point2 = vision_data["point2"]
        point3 = vision_data["point3"]
        summary = vision_data["summary"]

        # Step 2: Dense Embedding
        job.status = "embedding"
        job.progress_percent = 65
        job.status_message = f"Generating 384d dense vector embedding on {device_label}..."

        combined_text = (
            f"Image File: {job.original_filename}\n"
            f"Category: {job.category}\n"
            f"Summary: {summary}\n"
            f"1. Visual Content & Entities: {point1}\n"
            f"2. Text, Labels & Data: {point2}\n"
            f"3. Context, Domain & Purpose: {point3}"
        )

        if cuda_ok and hasattr(self.embed_model, "_model") and hasattr(self.embed_model._model, "to"):
            self.embed_model._model.to("cuda")

        embedding = self.embed_model.get_text_embedding(combined_text)
        dim = len(embedding) if embedding else 384

        # Step 3: Neo4j Indexing
        job.status = "indexing"
        job.progress_percent = 85
        job.status_message = "Writing (:Image), (:Face) nodes, and vector indexes to Neo4j..."

        if not self.driver:
            raise RuntimeError("Neo4j driver is not connected.")

        with self.driver.session() as session:
            session.run("""
                CREATE FULLTEXT INDEX image_text_index IF NOT EXISTS
                FOR (i:Image)
                ON EACH [i.description, i.filename, i.summary, i.point1, i.point2, i.point3]
            """)

            session.run(f"""
                CREATE VECTOR INDEX image_vector_index IF NOT EXISTS
                FOR (i:Image)
                ON (i.embedding)
                OPTIONS {{indexConfig: {{
                    `vector.dimensions`: {dim},
                    `vector.similarity_function`: 'cosine'
                }}}}
            """)

            session.run("""
                CREATE VECTOR INDEX face_vector_index IF NOT EXISTS
                FOR (f:Face)
                ON (f.embedding)
                OPTIONS {indexConfig: {
                    `vector.dimensions`: 512,
                    `vector.similarity_function`: 'cosine'
                }}
            """)

            image_id = str(uuid.uuid4())
            session.run("""
                MERGE (i:Image {filename: $filename})
                SET i.id = $id,
                    i.file_path = $file_path,
                    i.image_url = $image_url,
                    i.point1 = $point1,
                    i.point2 = $point2,
                    i.point3 = $point3,
                    i.summary = $summary,
                    i.description = $description,
                    i.category = $category,
                    i.embedding = $embedding,
                    i.face_count = $face_count,
                    i.created_at = $created_at
            """, {
                "filename": job.original_filename,
                "id": image_id,
                "file_path": str(dest_img_path),
                "image_url": image_url,
                "point1": point1,
                "point2": point2,
                "point3": point3,
                "summary": summary,
                "description": combined_text,
                "category": job.category,
                "embedding": embedding,
                "face_count": len(detected_faces),
                "created_at": time.time()
            })

            session.run("""
                MERGE (cat:Category {name: $category})
                WITH cat
                MATCH (i:Image {filename: $filename})
                MERGE (i)-[:IN_CATEGORY]->(cat)
                MERGE (cat)-[:HAS_IMAGE]->(i)
            """, {
                "category": job.category,
                "filename": job.original_filename
            })

            # Clean any previous faces on re-upload
            session.run("""
                MATCH (i:Image {filename: $filename})-[r:HAS_FACE]->(old_f:Face)
                DETACH DELETE old_f
            """, {"filename": job.original_filename})

            # Store each detected face as a (:Face) node linked via :HAS_FACE
            for face in detected_faces:
                face_id = str(uuid.uuid4())
                session.run("""
                    MATCH (i:Image {filename: $filename})
                    CREATE (f:Face {
                        id: $id,
                        bbox: $bbox,
                        det_score: $det_score,
                        embedding: $embedding,
                        crop_url: $crop_url,
                        created_at: $created_at
                    })
                    CREATE (i)-[:HAS_FACE]->(f)
                """, {
                    "filename": job.original_filename,
                    "id": face_id,
                    "bbox": face["bbox"],
                    "det_score": face["det_score"],
                    "embedding": face["embedding"],
                    "crop_url": face.get("crop_url", ""),
                    "created_at": time.time()
                })

        job.chunks_count = 1
        job.progress_percent = 100
        job.status_message = "Image analyzed and indexed successfully!"
        logger.info(f"Image '{job.original_filename}' successfully indexed with {len(detected_faces)} face(s) and 3 description points.")

    def get_indexed_images(self) -> List[Dict[str, Any]]:
        """Queries Neo4j for all indexed images and their 3 description points and face metadata."""
        if not self.driver:
            return []

        with self.driver.session() as session:
            try:
                result = session.run("""
                    MATCH (i:Image)
                    OPTIONAL MATCH (i)-[:HAS_FACE]->(f:Face)
                    WITH i, count(f) as face_count, collect(f.crop_url) as face_crops
                    RETURN i.id as id, i.filename as filename, i.image_url as image_url,
                           i.point1 as point1, i.point2 as point2, i.point3 as point3,
                           i.summary as summary, i.category as category, i.created_at as created_at,
                           face_count, face_crops
                    ORDER BY i.created_at DESC
                """)
                images = []
                for record in result:
                    images.append({
                        "id": record["id"] or "",
                        "filename": record["filename"],
                        "image_url": record["image_url"] or f"/images/{record['category']}/{record['filename']}",
                        "point1": record["point1"] or "",
                        "point2": record["point2"] or "",
                        "point3": record["point3"] or "",
                        "summary": record["summary"] or "",
                        "category": record["category"] or "General",
                        "face_count": record["face_count"] or 0,
                        "face_crops": [c for c in (record["face_crops"] or []) if c],
                        "created_at": record["created_at"] or 0
                    })
                return images
            except Exception as e:
                logger.warning(f"Failed to query indexed images: {e}")
                return []

    def delete_image(self, filename: str) -> bool:
        """Deletes an image and associated face nodes from Neo4j and disk."""
        if not self.driver:
            return False

        with self.driver.session() as session:
            try:
                rec = session.run("MATCH (i:Image {filename: $fn}) RETURN i.file_path as fp", {"fn": filename}).single()
                if rec and rec["fp"] and os.path.exists(rec["fp"]):
                    try:
                        os.remove(rec["fp"])
                    except Exception as e:
                        logger.warning(f"Failed to remove image file {rec['fp']}: {e}")

                session.run("""
                    MATCH (i:Image {filename: $fn})
                    OPTIONAL MATCH (i)-[:HAS_FACE]->(f:Face)
                    DETACH DELETE f, i
                """, {"fn": filename})
                return True
            except Exception as e:
                logger.error(f"Failed to delete image {filename}: {e}")
                return False
