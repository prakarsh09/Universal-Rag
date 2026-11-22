import os
import sys
import re
import json
import uuid
import asyncio
import traceback
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from dotenv import load_dotenv

# Ensure we can import from the parent directory
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import setup_global_settings, get_neo4j_driver
from llama_index.core import Settings, PromptTemplate
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.core.response_synthesizers import get_response_synthesizer
from llama_index.core.postprocessor.types import BaseNodePostprocessor
from llama_index.core.embeddings.utils import resolve_embed_model

try:
    from backend.document_worker import DocumentIngestionWorker
except ImportError:
    from document_worker import DocumentIngestionWorker

# Load environment variables
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

app = FastAPI(title="Universal Graph-RAG API")

# Mount uploaded_images folder for image rendering
images_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploaded_images")
os.makedirs(images_dir, exist_ok=True)
app.mount("/images", StaticFiles(directory=images_dir), name="images")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/ui") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Global model and database configuration
print("Setting up global LLM and embedding settings...")
llm = setup_global_settings()
embed_model = resolve_embed_model(Settings.embed_model)
print(f"Embedding model loaded: {embed_model.model_name}")

print("Connecting to Neo4j...")
driver = get_neo4j_driver()

try:
    with driver.session() as session:
        result = session.run("MATCH (c:Chunk) RETURN count(c) as cnt").single()
        print(f"  Connected to Neo4j. Found {result['cnt']} chunks.")
except Exception as e:
    print(f"  [Error] Failed to connect to Neo4j: {e}")

print("Initializing Background Document Ingestion Worker...")
doc_worker = DocumentIngestionWorker(driver=driver, embed_model=embed_model)

# Import retriever and reranker from parent module
import importlib
query_graph_module = importlib.import_module("3_query_graph")
HybridNeo4jRetriever = query_graph_module.HybridNeo4jRetriever
UniversalTableBoostReranker = query_graph_module.UniversalTableBoostReranker
HybridNeo4jImageRetriever = getattr(query_graph_module, "HybridNeo4jImageRetriever", None)
Neo4jFaceRetriever = getattr(query_graph_module, "Neo4jFaceRetriever", None)
classify_search_intent = getattr(query_graph_module, "classify_search_intent", None)

image_retriever = HybridNeo4jImageRetriever(driver=driver, embed_model=embed_model) if HybridNeo4jImageRetriever else None
face_retriever = Neo4jFaceRetriever(driver=driver) if Neo4jFaceRetriever and driver else None

from backend.face_engine import get_face_engine
face_engine = get_face_engine()

qa_prompt_tmpl = (
    "Context information is below.\n"
    "---------------------\n"
    "{context_str}\n"
    "---------------------\n"
    "Given the context information and not prior knowledge, answer the query accurately.\n\n"
    "Guidelines:\n"
    "1. Be factual and precise. If exact figures, dates, or specifications are mentioned, include them.\n"
    "2. Cite the source document name and section path for facts or figures mentioned.\n"
    "3. If multiple revisions, perspectives, or conflicting figures exist in the context, state them clearly and explain the distinction.\n"
    "4. If the context does not contain sufficient information to answer the question, state clearly what is known and what is missing.\n\n"
    "Query: {query_str}\n"
    "Answer: "
)

# Initialize retriever & reranker
reranker_type = os.getenv("RERANKER_TYPE", "smart-table")
top_n = int(os.getenv("RERANKER_TOP_N", "8"))

retriever = HybridNeo4jRetriever(
    driver=driver,
    embed_model=embed_model,
    vector_top_k=40 if reranker_type == "smart-table" else 20,
    fulltext_top_k=40 if reranker_type == "smart-table" else 20,
    rrf_k=60
)

if reranker_type == "smart-table":
    reranker = UniversalTableBoostReranker(top_n=top_n)
else:
    reranker = query_graph_module.DebugReranker(
        reranker_type=reranker_type,
        top_n=top_n,
        llm=llm
    )

class AdjacentContextExpander(BaseNodePostprocessor):
    """Postprocessor that queries Neo4j [:NEXT] relationships to dynamically fetch
    preceding and succeeding context chunks for retrieved text nodes.
    """
    def __init__(self, driver):
        super().__init__()
        self._driver = driver
        
    def _strip_raw_prefix(self, text):
        if not text:
            return ""
        parts = text.split("\n\n", 1)
        if len(parts) >= 2:
            return parts[1].strip()
        return text.strip()
        
    def _get_adjacent_contexts_batched(self, node_ids):
        if not node_ids:
            return {}
        
        results = {}
        with self._driver.session() as session:
            # 1. Query using [:NEXT] relationships
            records = session.run("""
                MATCH (c:Chunk)
                WHERE c.id IN $ids
                OPTIONAL MATCH (prev:Chunk)-[:NEXT]->(c)
                OPTIONAL MATCH (c)-[:NEXT]->(next:Chunk)
                RETURN c.id as id, prev.text as prev_text, next.text as next_text
            """, {"ids": node_ids})
            
            for record in records:
                nid = record["id"]
                prev_text = record["prev_text"]
                next_text = record["next_text"]
                if prev_text is not None or next_text is not None:
                    results[nid] = (prev_text, next_text)
            
            # 2. Fallback to doc_id and chunk_index if needed
            missing_ids = [nid for nid in node_ids if nid not in results]
            if missing_ids:
                records_fallback = session.run("""
                    MATCH (c:Chunk)
                    WHERE c.id IN $ids AND c.chunk_index IS NOT NULL AND c.doc_id IS NOT NULL
                    OPTIONAL MATCH (prev:Chunk {doc_id: c.doc_id, chunk_index: c.chunk_index - 1})
                    OPTIONAL MATCH (next:Chunk {doc_id: c.doc_id, chunk_index: c.chunk_index + 1})
                    RETURN c.id as id, prev.text as prev_text, next.text as next_text
                """, {"ids": missing_ids})
                
                for record in records_fallback:
                    nid = record["id"]
                    prev_text = record["prev_text"]
                    next_text = record["next_text"]
                    if prev_text is not None or next_text is not None:
                        results[nid] = (prev_text, next_text)
                        
        return results

    def _postprocess_nodes(self, nodes, query_bundle=None):
        text_node_map = {}
        for nws in nodes:
            node = nws.node
            chunk_type = node.metadata.get("chunk_type")
            if chunk_type == "text":
                text_node_map[node.node_id] = node
                
        adjacent_contexts = self._get_adjacent_contexts_batched(list(text_node_map.keys()))
        
        for node_id, node in text_node_map.items():
            if node_id in adjacent_contexts:
                prev_text, next_text = adjacent_contexts[node_id]
                
                prev_body = self._strip_raw_prefix(prev_text)
                next_body = self._strip_raw_prefix(next_text)
                
                if prev_body or next_body:
                    clean_text = node.get_content()
                    parts = clean_text.split("-----------------------------------------\n", 1)
                    if len(parts) >= 2:
                        header = parts[0] + "-----------------------------------------\n"
                        current_body = parts[1].strip()
                        
                        merged_body = ""
                        if prev_body:
                            merged_body += f"... [Preceding Context] ...\n{prev_body}\n\n"
                        merged_body += current_body
                        if next_body:
                            merged_body += f"\n\n{next_body}\n... [Succeeding Context] ..."
                            
                        node.set_content(header + merged_body)
                        
        return nodes

# Build streaming query engine
qa_tmpl = PromptTemplate(qa_prompt_tmpl)
response_synthesizer = get_response_synthesizer(
    llm=llm,
    response_mode="compact",
    text_qa_template=qa_tmpl,
    streaming=True
)

query_engine = RetrieverQueryEngine(
    retriever=retriever,
    response_synthesizer=response_synthesizer,
    node_postprocessors=[reranker, AdjacentContextExpander(driver)]
)

class StatusResponse(BaseModel):
    neo4j_connected: bool
    neo4j_chunks: int
    llm_model: str
    reranker_type: str
    top_n: int

@app.get("/api/status", response_model=StatusResponse)
def get_status():
    """Returns system status, active models, and Neo4j connection health."""
    neo4j_ok = False
    chunks_count = 0
    try:
        with driver.session() as session:
            res = session.run("MATCH (c:Chunk) RETURN count(c) as cnt").single()
            chunks_count = res["cnt"]
            neo4j_ok = True
    except Exception:
        pass
        
    return StatusResponse(
        neo4j_connected=neo4j_ok,
        neo4j_chunks=chunks_count,
        llm_model=os.getenv("LLM_MODEL_NAME", "Unknown"),
        reranker_type=reranker_type,
        top_n=top_n
    )

@app.post("/api/documents/upload")
async def upload_document(file: UploadFile = File(...), category: str = Form("General")):
    """Uploads a document (.pdf, .md, .txt) and enqueues it for background graph ingestion."""
    filename = file.filename
    if not filename:
        raise HTTPException(status_code=400, detail="No file selected")
    
    ext = os.path.splitext(filename)[1].lower()
    allowed_types = [".pdf", ".md", ".markdown", ".txt", ".jpeg", ".jpg", ".png", ".webp"]
    if ext not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed types: PDF, Markdown, Text, and Images (JPEG, PNG)"
        )
    
    category = category.strip() if category else "General"
    category = re.sub(r'[/\\:*?"<>|]', '_', category)
    
    # Save image files to image_dir, documents to input_dir
    is_image = ext in [".jpeg", ".jpg", ".png", ".webp"]
    if is_image:
        upload_dir = os.path.join(doc_worker.image_dir, category)
    else:
        upload_dir = os.path.join(doc_worker.input_dir, category)
    os.makedirs(upload_dir, exist_ok=True)
    
    safe_filename = os.path.basename(filename)
    dest_path = os.path.join(upload_dir, safe_filename)
    
    try:
        content = await file.read()
        with open(dest_path, "wb") as f:
            f.write(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")
        
    # Enqueue into background worker
    job = doc_worker.enqueue(
        file_path=dest_path,
        original_filename=safe_filename,
        category=category
    )
    
    return {
        "success": True,
        "message": f"'{safe_filename}' successfully queued for graph indexing.",
        "job": job
    }

@app.get("/api/documents/queue")
def get_documents_queue():
    """Returns worker status, actively processing document, queue list, and recent history."""
    return doc_worker.get_status()

@app.get("/api/documents")
def get_indexed_documents():
    """Returns all documents indexed in the Neo4j Knowledge Graph."""
    docs = doc_worker.get_indexed_documents()
    return {"documents": docs, "total": len(docs)}

@app.delete("/api/documents/{document_name}")
def delete_document(document_name: str):
    """Deletes a document and its chunks from the Neo4j Knowledge Graph."""
    success = doc_worker.delete_document(document_name)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete document from graph.")
    return {"success": True, "message": f"Document '{document_name}' deleted from graph."}

@app.get("/api/images")
def get_indexed_images():
    """Returns all indexed images and their 3 description points."""
    images = doc_worker.get_indexed_images()
    return {"images": images, "total": len(images)}

@app.delete("/api/images/{filename}")
def delete_image(filename: str):
    """Deletes an image from Neo4j Knowledge Graph and local storage."""
    success = doc_worker.delete_image(filename)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete image from graph.")
    return {"success": True, "message": f"Image '{filename}' deleted from graph."}


@app.post("/api/search/reverse-image")
async def reverse_image_search(
    file: Optional[UploadFile] = File(None),
    temp_image_id: Optional[str] = Form(None),
    selected_face_index: Optional[int] = Form(None),
    min_similarity: float = Form(0.48),
    top_k: int = Form(6)
):
    """Performs Reverse Image Search:
    - Case 1: 1 Face Detected -> 512d ArcFace vector cosine search (threshold >= min_similarity)
    - Case 2: Multiple Faces -> Returns face crops and boxes for user selection / Google Photos picker
    - Case 3: No Faces Detected -> Forwards image to local Ollama VLM (gemma4:e2b) for 3 description points,
              followed by 384d hybrid visual retrieval across existing image nodes.
    """
    temp_dir = os.path.join(images_dir, "_temp_searches")
    os.makedirs(temp_dir, exist_ok=True)

    img_path = None

    if file is not None:
        filename = file.filename or "query.jpg"
        ext = os.path.splitext(filename)[1].lower()
        if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
            raise HTTPException(status_code=400, detail="Invalid image format. Supported: JPEG, PNG, WebP.")
        safe_id = f"{uuid.uuid4().hex[:12]}_{re.sub(r'[^a-zA-Z0-9._-]', '_', filename)}"
        img_path = os.path.join(temp_dir, safe_id)
        content = await file.read()
        with open(img_path, "wb") as f:
            f.write(content)
        temp_image_id = safe_id
    elif temp_image_id:
        img_path = os.path.join(temp_dir, os.path.basename(temp_image_id))
        if not os.path.exists(img_path):
            raise HTTPException(status_code=404, detail="Session image expired or not found.")
    else:
        raise HTTPException(status_code=400, detail="Either file or temp_image_id must be provided.")

    # 1. Run Face Detection & ArcFace Embedding
    faces = face_engine.detect_and_embed(img_path)

    # 2. Branch A: Multiple Faces and user hasn't selected one yet
    if len(faces) > 1 and selected_face_index is None:
        return {
            "status": "multiple_faces_detected",
            "search_type": "face_disambiguation_required",
            "temp_image_id": temp_image_id,
            "query_image_url": f"/images/_temp_searches/{temp_image_id}",
            "faces_detected": len(faces),
            "faces": [
                {
                    "face_index": f["index"],
                    "bbox": f["bbox"],
                    "det_score": round(f["det_score"], 3),
                    "crop_base64": f["crop_base64"]
                }
                for f in faces
            ]
        }

    # 3. Branch B: Exactly 1 face or user selected specific face / "Search All" (-1)
    if faces and (len(faces) == 1 or selected_face_index is not None):
        if not face_retriever:
            raise HTTPException(status_code=500, detail="Face retriever is not connected to Neo4j.")

        if selected_face_index is not None and selected_face_index == -1:
            # Search all detected faces (union)
            face_vectors = [f["embedding"] for f in faces]
            matched_images = await asyncio.to_thread(
                face_retriever.retrieve_multi, face_vectors, min_similarity, top_k
            )
            selected_crop = ""
        else:
            idx = selected_face_index if selected_face_index is not None and 0 <= selected_face_index < len(faces) else 0
            target_face = faces[idx]
            face_vector = target_face["embedding"]
            selected_crop = target_face["crop_base64"]
            matched_images = await asyncio.to_thread(
                face_retriever.retrieve, face_vector, min_similarity, top_k
            )

        return {
            "status": "completed",
            "search_type": "face_search",
            "temp_image_id": temp_image_id,
            "query_image_url": f"/images/_temp_searches/{temp_image_id}",
            "faces_detected": len(faces),
            "selected_face_crop": selected_crop,
            "min_similarity": min_similarity,
            "matches": matched_images,
            "total_matches": len(matched_images)
        }

    # 4. Branch C: No Faces Detected -> Fallback to Local VLM (gemma4:e2b) visual points + hybrid search
    vision_data = await asyncio.to_thread(doc_worker._extract_ollama_vision_description, Path(img_path))
    vlm_summary = vision_data.get("summary", "")
    p1 = vision_data.get("point1", "")
    p2 = vision_data.get("point2", "")
    p3 = vision_data.get("point3", "")

    search_query = f"{vlm_summary} {p1} {p2}".strip()
    matched_images = []
    if image_retriever and search_query:
        matched_images = await asyncio.to_thread(image_retriever.retrieve, search_query)

    return {
        "status": "completed",
        "search_type": "vlm_semantic_search",
        "temp_image_id": temp_image_id,
        "query_image_url": f"/images/_temp_searches/{temp_image_id}",
        "faces_detected": 0,
        "vlm_description": {
            "summary": vlm_summary,
            "point1": p1,
            "point2": p2,
            "point3": p3
        },
        "matches": matched_images,
        "total_matches": len(matched_images)
    }


def rewrite_query(message: str, history: list) -> str:
    """Uses LLM memory to rewrite conversational follow-ups into standalone search queries."""
    if not history:
        return message
        
    history_str = ""
    for turn in history[-5:]:
        role = "User" if turn["role"] == "user" else "Assistant"
        content = turn["content"]
        content_clean = re.sub(r'\|.*\|', '[Table omitted]', content)
        history_str += f"{role}: {content_clean}\n"
        
    rewrite_prompt = (
        "You are an AI assistant that reformulates user follow-up questions for a search engine.\n\n"
        "Your task is to rewrite the follow-up question into a standalone search query ONLY if it contains ambiguous pronouns or conversational shortcuts (like 'it', 'ones', 'what about...', 'these', 'those', 'they').\n\n"
        "Rules:\n"
        "1. Do NOT add specific section numbers, document names, or page numbers unless explicitly mentioned by the user.\n"
        "2. If the question is already standalone, output it exactly as it is without modifying a single word.\n"
        "3. Do NOT answer the question. Only output the rewritten search query.\n\n"
        f"Conversation History:\n{history_str}\n"
        f"Follow-up Question: {message}\n"
        "Standalone Search Query: "
    )
    
    try:
        response = llm.complete(rewrite_prompt)
        rewritten = str(response).strip().strip('"\'')
        return rewritten
    except Exception as e:
        print(f"  [Warning] Query rewrite failed: {e}. Using original message.")
        return message

@app.websocket("/api/chat")
async def websocket_chat_endpoint(websocket: WebSocket):
    """WebSocket endpoint that receives user queries, emits source citations, and streams tokens."""
    await websocket.accept()
    print("  [WebSocket] Client connected.")
    
    try:
        while True:
            data_str = await websocket.receive_text()
            data = json.loads(data_str)
            
            message = data.get("message", "").strip()
            history = data.get("history", [])
            
            if not message:
                continue
                
            search_query = await asyncio.to_thread(rewrite_query, message, history)
            
            # Classify query intent: image, document, or both
            intent = "document"
            if classify_search_intent:
                intent = await asyncio.to_thread(classify_search_intent, search_query, llm)
            print(f"  [Intent Classification] Query: \"{search_query}\" -> Intent: {intent}")
            
            if intent == "image":
                await websocket.send_json({
                    "type": "status",
                    "content": f"Searching visual knowledge graph for images matching: \"{search_query}\"..."
                })
                
                matched_images = []
                if image_retriever:
                    matched_images = await asyncio.to_thread(image_retriever.retrieve, search_query)
                    for rank_idx, img in enumerate(matched_images, start=1):
                        img["rank"] = rank_idx
                    
                await websocket.send_json({
                    "type": "image_results",
                    "content": matched_images
                })
                
                if matched_images:
                    img_summary_parts = []
                    for img in matched_images[:3]:
                        p1 = img.get('point1', '')
                        p2 = img.get('point2', '')
                        p3 = img.get('point3', '')
                        img_summary_parts.append(f"Image '{img.get('filename')}':\n- Visual: {p1}\n- Text: {p2}\n- Context: {p3}")
                    
                    synth_prompt = (
                        "You are an assistant presenting visual search results from the knowledge graph.\n"
                        f"User Query: {search_query}\n"
                        f"Matched Images:\n" + "\n\n".join(img_summary_parts) + "\n\n"
                        "Provide a concise, direct 2-3 sentence overview of these matched images and how they answer the user's query. "
                        "Mention the filenames in bold."
                    )
                    try:
                        llm_resp = await asyncio.to_thread(llm.complete, synth_prompt)
                        summary_text = str(llm_resp).strip()
                        await websocket.send_json({"type": "token", "content": summary_text})
                    except Exception as synth_err:
                        await websocket.send_json({
                            "type": "token",
                            "content": f"Found {len(matched_images)} matching image(s) for your search."
                        })
                else:
                    await websocket.send_json({
                        "type": "token",
                        "content": f"No visual images matching \"{search_query}\" were found in the knowledge graph. You can upload JPEG images in the Documents & Media tab."
                    })
                
                await websocket.send_json({"type": "done"})
                continue
                
            elif intent == "both":
                await websocket.send_json({
                    "type": "status",
                    "content": f"Searching documents and visual media for: \"{search_query}\"..."
                })
                if image_retriever:
                    matched_images = await asyncio.to_thread(image_retriever.retrieve, search_query)
                    for rank_idx, img in enumerate(matched_images, start=1):
                        img["rank"] = rank_idx
                    if matched_images:
                        await websocket.send_json({
                            "type": "image_results",
                            "content": matched_images
                        })

            else:
                await websocket.send_json({
                    "type": "status",
                    "content": f"Searching knowledge graph for: \"{search_query}\"..."
                })
            
            try:
                response = await asyncio.to_thread(query_engine.query, search_query)
                
                # Send source metadata
                sources = []
                for score_node in response.source_nodes:
                    node = score_node.node
                    sources.append({
                        "id": node.node_id,
                        "score": float(score_node.score) if score_node.score is not None else 0.0,
                        "document_name": node.metadata.get("document_name", "Unknown"),
                        "category": node.metadata.get("category", "General"),
                        "header_path": re.sub(r'<[^>]+>', '', node.metadata.get("header_path", "/")),
                        "chunk_type": node.metadata.get("chunk_type", "text")
                    })
                
                await websocket.send_json({
                    "type": "sources",
                    "content": sources
                })
                
                # Stream synthesized answer tokens
                for token in response.response_gen:
                    await websocket.send_json({
                        "type": "token",
                        "content": token
                    })
                    
                await websocket.send_json({"type": "done"})
                
            except Exception as query_err:
                err_msg = f"Error during query execution: {query_err}"
                print(f"  [Error] {err_msg}")
                traceback.print_exc()
                await websocket.send_json({
                    "type": "error",
                    "content": err_msg
                })
                
    except WebSocketDisconnect:
        print("  [WebSocket] Client disconnected.")
    except Exception as e:
        print(f"  [WebSocket Exception] {e}")
        traceback.print_exc()

@app.get("/")
def redirect_root():
    return RedirectResponse(url="/ui/")

@app.get("/ui")
def redirect_ui_noslash():
    return RedirectResponse(url="/ui/")

# Mount static UI folder
ui_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
if os.path.exists(ui_dir):
    app.mount("/ui", StaticFiles(directory=ui_dir, html=True), name="ui")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5005, reload=True)
