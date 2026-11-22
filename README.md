# Universal Graph-RAG Platform

An enterprise-grade, domain-agnostic **Hybrid Graph-RAG** platform built with **LlamaIndex, Neo4j, and FastAPI**.

## Key Features
- **Table-Aware Layout Parsing**: Uses Marker to convert complex PDFs into structured Markdown.
- **Table-to-Narrative Synthesis**: Automatically parses Markdown tables and generates natural language sentence summaries row-by-row, keeping dense vectors and BM25 search highly accurate on tabular data.
- **Structural Breadcrumb Context**: Tracks header hierarchy (`#` through `####`) to maintain context paths for all chunks.
- **Hybrid Search + Reciprocal Rank Fusion (RRF)**: Combines 384-dimensional dense vector embeddings (`BAAI/bge-small-en-v1.5`) with Lucene BM25 fulltext search.
- **Graph-Based Adjacent Context Expansion**: Traverses Neo4j `(:Chunk)-[:NEXT]->(:Chunk)` edges to dynamically pull preceding and succeeding sentences for retrieved text nodes.
- **Cross-Encoder Reranking**: Uses `BAAI/bge-reranker-base` to rerank and boost relevant quantitative/tabular nodes.
- **Conversational Memory**: Rewrites multi-turn chat follow-ups into standalone search queries.
- **Streaming Web UI**: Glassmorphic dark mode UI with 60fps liquid background animations, real-time token streaming, and source citations.

---

## Project Structure
```
Generic llama index rag/
├── input_docs/               # Put your raw PDF files here (supports subfolders)
├── markdown_output/          # Converted markdown files
├── 1_convert_documents.py    # Converts PDFs to Markdown via Marker
├── 2_build_graph.py          # Chunks, embeds, and builds Neo4j Graph + Indexes
├── 3_query_graph.py          # Interactive CLI query engine
├── config.py                 # LLM, embedding, and Neo4j connection settings
├── utils.py                  # Table narrative generator & semantic chunker
├── backend/
│   ├── main.py               # FastAPI backend with WebSocket streaming
│   ├── Dockerfile
│   └── requirements.txt
├── ui/
│   ├── index.html            # Modern chat interface
│   ├── style.css             # Glassmorphism dark theme
│   ├── app.js                # WebSocket & streaming logic
│   ├── Dockerfile
│   └── nginx.conf
├── docker-compose.yml        # Multi-container setup (Neo4j, Backend, UI)
├── requirements.txt          # Python dependencies
└── .env                      # Environment variables
```

---

## Quick Start Guide

### Option 1: Docker Compose (All-In-One)
```bash
docker compose up --build -d
```
- UI available at: `http://localhost:3006`
- Backend API at: `http://localhost:5005`
- Neo4j Browser at: `http://localhost:7474` (User: `neo4j`, Password: `password`)

### Option 2: Local Python Execution

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Start Neo4j**:
   Ensure Neo4j is running locally (e.g. via Docker):
   ```bash
   docker run -d --name neo4j-rag -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/password neo4j:5.26.0-community
   ```

3. **Convert PDF Documents**:
   Place your PDFs in `input_docs/` and run:
   ```bash
   python 1_convert_documents.py
   ```

4. **Build Knowledge Graph**:
   ```bash
   python 2_build_graph.py
   ```

5. **Start Web Backend & Chat UI**:
   ```bash
   python -m uvicorn backend.main:app --host 0.0.0.0 --port 5005 --reload
   ```
   Open `http://localhost:5005/ui/index.html` in your browser.
