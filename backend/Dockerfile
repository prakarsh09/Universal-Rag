FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Pre-cache HuggingFace models for fast startup
RUN python -c "\
from llama_index.embeddings.huggingface import HuggingFaceEmbedding; \
HuggingFaceEmbedding(model_name='BAAI/bge-small-en-v1.5'); \
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('BAAI/bge-reranker-base')\
"

# Copy root modules & environment
COPY config.py utils.py 3_query_graph.py .env ./

# Copy backend application
COPY backend/ ./backend/

ENV PYTHONPATH=/app
EXPOSE 5005

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "5005"]
