import os
# Force huggingface offline mode if models already downloaded
os.environ.setdefault("HF_HUB_OFFLINE", "1")
import sys
import re
import traceback

# Ensure UTF-8 output on Windows console
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from llama_index.core import Settings, PromptTemplate
from llama_index.core.schema import TextNode, NodeWithScore, QueryBundle
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.core.response_synthesizers import get_response_synthesizer
from llama_index.core.retrievers import BaseRetriever
from llama_index.core.postprocessor import LLMRerank, SentenceTransformerRerank
from llama_index.core.postprocessor.types import BaseNodePostprocessor
from config import setup_global_settings, get_neo4j_driver
import torch

class HybridNeo4jRetriever(BaseRetriever):
    """Custom retriever that combines Neo4j vector search and full-text search
    using Reciprocal Rank Fusion (RRF) for optimal retrieval across any document domain.
    """
    def __init__(self, driver, embed_model, vector_top_k=20, fulltext_top_k=20, rrf_k=60):
        super().__init__()
        self._driver = driver
        self._embed_model = embed_model
        self._vector_top_k = vector_top_k
        self._fulltext_top_k = fulltext_top_k
        self._rrf_k = rrf_k
    
    def _retrieve(self, query_bundle: QueryBundle):
        query_text = query_bundle.query_str
        
        # 1. Vector search
        query_embedding = self._embed_model.get_text_embedding(query_text)
        vector_results = self._vector_search(query_embedding)
        
        # 2. Full-text search
        fulltext_results = self._fulltext_search(query_text)
        
        # 3. Reciprocal Rank Fusion
        fused = self._rrf_fuse(vector_results, fulltext_results)
        
        return fused
    
    def _vector_search(self, embedding):
        """Performs cosine similarity vector search on Chunk embeddings."""
        results = []
        with self._driver.session() as session:
            records = session.run("""
                CALL db.index.vector.queryNodes('chunk_vector_index', $top_k, $embedding)
                YIELD node, score
                RETURN node.id AS id, node.text AS text, node.category AS category,
                       node.document_name AS doc_name, node.header_path AS header_path,
                       node.chunk_type AS chunk_type, score
                ORDER BY score DESC
                LIMIT $limit
            """, {"top_k": self._vector_top_k * 2, "embedding": embedding, "limit": self._vector_top_k})
            
            for rank, record in enumerate(records):
                results.append({
                    "id": record["id"],
                    "text": record["text"],
                    "category": record["category"],
                    "doc_name": record["doc_name"],
                    "header_path": record["header_path"],
                    "chunk_type": record["chunk_type"],
                    "score": record["score"],
                    "rank": rank + 1,
                    "source": "vector"
                })
        return results
    
    def _fulltext_search(self, query_text):
        """Performs native Neo4j full-text (BM25) search on Chunk.text."""
        escaped = self._escape_lucene(query_text)
        results = []
        with self._driver.session() as session:
            try:
                records = session.run("""
                    CALL db.index.fulltext.queryNodes('chunk_text_index', $query)
                    YIELD node, score
                    RETURN node.id AS id, node.text AS text, node.category AS category,
                           node.document_name AS doc_name, node.header_path AS header_path,
                           node.chunk_type AS chunk_type, score
                    ORDER BY score DESC
                    LIMIT $limit
                """, {"query": escaped, "limit": self._fulltext_top_k})
                
                for rank, record in enumerate(records):
                    results.append({
                        "id": record["id"],
                        "text": record["text"],
                        "category": record["category"],
                        "doc_name": record["doc_name"],
                        "header_path": record["header_path"],
                        "chunk_type": record["chunk_type"],
                        "score": record["score"],
                        "rank": rank + 1,
                        "source": "fulltext"
                    })
            except Exception as e:
                print(f"  [Warning] Full-text search error: {e}")
        return results
    
    def _escape_lucene(self, text):
        """Escapes special Lucene query characters."""
        special_chars = r'+-&|!(){}[]^"~*?:\/'
        escaped = ""
        for ch in text:
            if ch in special_chars:
                escaped += "\\" + ch
            else:
                escaped += ch
        return escaped
    
    def _rrf_fuse(self, vector_results, fulltext_results):
        """Reciprocal Rank Fusion to combine vector and full-text search results."""
        scores = {}  # id -> rrf_score
        data = {}    # id -> result dict
        
        for r in vector_results:
            rid = r["id"]
            scores[rid] = scores.get(rid, 0) + 1.0 / (self._rrf_k + r["rank"])
            data[rid] = r
        
        for r in fulltext_results:
            rid = r["id"]
            scores[rid] = scores.get(rid, 0) + 1.0 / (self._rrf_k + r["rank"])
            if rid not in data:
                data[rid] = r
        
        # Sort by fused score descending
        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        
        # Convert to NodeWithScore objects
        nodes_with_scores = []
        for rid in sorted_ids:
            r = data[rid]
            node = TextNode(
                text=r["text"],
                id_=r["id"],
                metadata={
                    "category": r["category"],
                    "document_name": r["doc_name"],
                    "header_path": r["header_path"],
                    "chunk_type": r["chunk_type"]
                }
            )
            nws = NodeWithScore(node=node, score=scores[rid])
            nodes_with_scores.append(nws)
        
        return nodes_with_scores

class UniversalTableBoostReranker(BaseNodePostprocessor):
    """Custom postprocessor that reranks chunks using a cross-encoder, 
    boosts table chunks for quantitative/comparative queries, and deduplicates identical bodies.
    """
    def __init__(self, top_n=8):
        super().__init__()
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self._internal = SentenceTransformerRerank(
            model="BAAI/bge-reranker-base",
            top_n=100,  # get all scores first
            device=device
        )
        self._top_n = top_n
        
    def _clean_node_text(self, node):
        text = node.get_content()
        parts = text.split("\n", 1)
        if len(parts) < 2:
            return
            
        prefix = parts[0]
        body = parts[1]
        
        # Parse metadata from prefix
        doc_match = re.search(r'Document:\s*([^|]+)', prefix)
        cat_match = re.search(r'Category:\s*([^|]+)', prefix)
        context_match = re.search(r'Context:\s*(.+)', prefix)
        
        doc = doc_match.group(1).strip() if doc_match else "Unknown"
        category = cat_match.group(1).strip() if cat_match else "General"
        header_path = context_match.group(1).strip() if context_match else ""
        
        # Clean HTML span tags
        header_path_clean = re.sub(r'<[^>]+>', '', header_path)
        header_path_clean = "/".join([p.strip() for p in header_path_clean.split("/") if p.strip()])
        
        # Build clean structured text
        clean_text = (
            f"SOURCE DOCUMENT: {doc}\n"
            f"CATEGORY: {category}\n"
            f"SECTION PATH: {header_path_clean}\n"
            f"-----------------------------------------\n"
            f"{body.strip()}"
        )
        
        node.set_content(clean_text)

    def _postprocess_nodes(self, nodes, query_bundle=None):
        query_text = query_bundle.query_str.lower() if query_bundle else ""
        
        # 1. Get base scores from cross-encoder
        scored_nodes = self._internal.postprocess_nodes(nodes, query_bundle)
        
        # 2. Apply Generic Quantitative / Tabular Boosting
        # Triggers on quantitative keywords, numbers, or comparative phrasing
        quant_keywords = ["table", "list", "compare", "comparison", "summary", "stats", "statistics", 
                          "percentage", "%", "total", "amount", "budget", "revenue", "cost", "price", 
                          "rate", "metric", "count", "how many", "how much"]
        is_quant_query = any(kw in query_text for kw in quant_keywords) or bool(re.search(r'\d+', query_text))
        
        if is_quant_query:
            for nws in scored_nodes:
                node = nws.node
                chunk_type = node.metadata.get("chunk_type", "")
                if chunk_type == "table":
                    boost = 0.25
                    old_score = nws.score if nws.score is not None else 0.0
                    nws.score = old_score + boost
            
            # Re-sort nodes by updated score
            scored_nodes = sorted(scored_nodes, key=lambda x: x.score if x.score is not None else -9999.0, reverse=True)
            
        # 3. Deduplicate nodes by body content
        unique_nodes = []
        seen_bodies = set()
        for nws in scored_nodes:
            text = nws.node.get_content()
            parts = text.split("\n", 1)
            body = parts[1].strip() if len(parts) >= 2 else text.strip()
            
            normalized_body = " ".join(body.lower().split())
            body_sig = normalized_body[:150]
            
            if body_sig not in seen_bodies:
                seen_bodies.add(body_sig)
                unique_nodes.append(nws)
                
        # 4. Slice to top_n
        filtered = unique_nodes[:self._top_n]
        
        # 5. Clean up the node texts for the selected nodes
        for i, n in enumerate(filtered):
            self._clean_node_text(n.node)
            
        return filtered

class DebugReranker(BaseNodePostprocessor):
    """Debug wrapper that can rerank using LLM, SentenceTransformers, or None."""
    def __init__(self, reranker_type="none", top_n=8, llm=None):
        super().__init__()
        self._reranker_type = reranker_type.lower()
        self._top_n = top_n
        device = "cuda" if torch.cuda.is_available() else "cpu"
        
        if self._reranker_type == "llm":
            self._internal = LLMRerank(choice_batch_size=5, top_n=top_n, llm=llm)
        elif self._reranker_type == "sentence-transformer":
            self._internal = SentenceTransformerRerank(model="BAAI/bge-reranker-base", top_n=top_n, device=device)
        else:
            self._internal = None

    def _postprocess_nodes(self, nodes, query_bundle=None):
        if self._reranker_type in ["llm", "sentence-transformer"] and self._internal:
            filtered = self._internal.postprocess_nodes(nodes, query_bundle)
        else:
            filtered = nodes[:self._top_n]
        return filtered

def query_knowledge_graph():
    """Interactive query engine with hybrid retrieval and cross-encoder reranking."""
    print("Setting up global LLM and embedding settings...")
    llm = setup_global_settings()
    
    from llama_index.core.embeddings.utils import resolve_embed_model
    embed_model = resolve_embed_model(Settings.embed_model)
    print(f"Embedding model: {embed_model.model_name}")
    
    print("Connecting to Neo4j...")
    driver = get_neo4j_driver()
    
    with driver.session() as session:
        result = session.run("MATCH (c:Chunk) RETURN count(c) as cnt").single()
        print(f"  Found {result['cnt']} chunks in database.")
    
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
        reranker = DebugReranker(reranker_type=reranker_type, top_n=top_n, llm=llm)
    
    # Universal QA Prompt Template
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
    qa_tmpl = PromptTemplate(qa_prompt_tmpl)
    
    response_synthesizer = get_response_synthesizer(
        llm=llm,
        response_mode="compact",
        text_qa_template=qa_tmpl
    )
    
    query_engine = RetrieverQueryEngine(
        retriever=retriever,
        response_synthesizer=response_synthesizer,
        node_postprocessors=[reranker]
    )
    
    print("\n" + "="*60)
    print("  Universal Graph-RAG Query Engine (Hybrid Retrieval)")
    print("  Type 'exit' or 'quit' to stop.")
    print("="*60 + "\n")
    
    while True:
        user_query = input("Ask a question: ")
        if user_query.lower() in ['exit', 'quit']:
            break
        
        if not user_query.strip():
            continue
        
        print(f"\nQuerying: \"{user_query}\"")
        try:
            response = query_engine.query(user_query)
            print("\n" + "="*60)
            print("ANSWER:")
            print("-"*60)
            print(response)
            print("="*60 + "\n")
        except Exception as e:
            print("Error querying:")
            traceback.print_exc()
    
    driver.close()
    print("Goodbye!")

if __name__ == "__main__":
    query_knowledge_graph()


class HybridNeo4jImageRetriever:
    """Retriever that performs vector search and fulltext search on (:Image) nodes
    using Reciprocal Rank Fusion (RRF) for natural language visual queries.
    """
    def __init__(self, driver, embed_model, top_k=6, rrf_k=60):
        self._driver = driver
        self._embed_model = embed_model
        self._top_k = top_k
        self._rrf_k = rrf_k

    def retrieve(self, query_str: str) -> list:
        if not self._driver or not self._embed_model:
            return []

        query_embedding = self._embed_model.get_text_embedding(query_str)
        vector_results = self._vector_search(query_embedding)
        fulltext_results = self._fulltext_search(query_str)
        return self._rrf_fuse(vector_results, fulltext_results)

    def _vector_search(self, embedding):
        results = []
        try:
            with self._driver.session() as session:
                records = session.run("""
                    CALL db.index.vector.queryNodes('image_vector_index', $top_k, $embedding)
                    YIELD node, score
                    RETURN node.id as id, node.filename as filename, node.image_url as image_url,
                           node.point1 as point1, node.point2 as point2, node.point3 as point3,
                           node.summary as summary, node.category as category, score
                    ORDER BY score DESC
                    LIMIT $top_k
                """, {"top_k": self._top_k * 2, "embedding": embedding})
                for r in records:
                    results.append(dict(r))
        except Exception:
            pass
        return results

    def _fulltext_search(self, query_str: str):
        results = []
        clean_terms = re.sub(r'[^a-zA-Z0-9\s]', ' ', query_str).split()
        if not clean_terms:
            return results
        search_phrase = " OR ".join([f"{term}*" for term in clean_terms if len(term) >= 2])
        if not search_phrase:
            return results

        try:
            with self._driver.session() as session:
                records = session.run("""
                    CALL db.index.fulltext.queryNodes('image_text_index', $phrase)
                    YIELD node, score
                    RETURN node.id as id, node.filename as filename, node.image_url as image_url,
                           node.point1 as point1, node.point2 as point2, node.point3 as point3,
                           node.summary as summary, node.category as category, score
                    ORDER BY score DESC
                    LIMIT $top_k
                """, {"phrase": search_phrase, "top_k": self._top_k * 2})
                for r in records:
                    results.append(dict(r))
        except Exception:
            pass
        return results

    def _rrf_fuse(self, vector_results, fulltext_results):
        scores = {}
        node_map = {}
        for rank, item in enumerate(vector_results):
            nid = item["filename"]
            node_map[nid] = item
            scores[nid] = scores.get(nid, 0.0) + (1.0 / (self._rrf_k + rank + 1))

        for rank, item in enumerate(fulltext_results):
            nid = item["filename"]
            if nid not in node_map:
                node_map[nid] = item
            scores[nid] = scores.get(nid, 0.0) + (1.0 / (self._rrf_k + rank + 1))

        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        final_list = []
        for nid in sorted_ids[:self._top_k]:
            item = node_map[nid]
            item["rrf_score"] = scores[nid]
            final_list.append(item)

        # If fewer than 3 images matched, fetch additional indexed images up to 3 (or exact count if < 3 exist)
        if len(final_list) < 3:
            existing_filenames = set(item["filename"] for item in final_list)
            try:
                with self._driver.session() as session:
                    records = session.run("""
                        MATCH (i:Image)
                        WHERE NOT i.filename IN $existing
                        RETURN i.id as id, i.filename as filename, i.image_url as image_url,
                               i.point1 as point1, i.point2 as point2, i.point3 as point3,
                               i.summary as summary, i.category as category, 0.0 as score
                        ORDER BY i.created_at DESC
                        LIMIT $needed
                    """, {"existing": list(existing_filenames), "needed": 3 - len(final_list)})
                    for r in records:
                        item = dict(r)
                        item["rrf_score"] = 0.005
                        final_list.append(item)
            except Exception:
                pass

        return final_list


def classify_search_intent(query: str, llm=None) -> str:
    """Classifies user query intent: 'image', 'document', or 'both'."""
    q_lower = query.lower()

    image_keywords = [
        "image", "images", "photo", "photos", "picture", "pictures",
        "diagram", "diagrams", "chart", "charts", "figure", "figures",
        "visual", "visuals", "screenshot", "screenshots", "pic", "pics",
        "show me image", "show image", "display image", "find picture", "look like"
    ]

    doc_keywords = [
        "section", "article", "page", "paragraph", "document", "regulation",
        "constitution", "clause", "tariff", "act", "law", "provision", "table"
    ]

    has_image_kw = any(kw in q_lower for kw in image_keywords)
    has_doc_kw = any(kw in q_lower for kw in doc_keywords)

    if has_image_kw and not has_doc_kw:
        return "image"
    if has_doc_kw and not has_image_kw:
        return "document"
    if has_image_kw and has_doc_kw:
        return "both"

    if re.search(r'\b(show|find|search|display|view)\b.*\b(image|picture|photo|chart|diagram|graph)\b', q_lower):
        return "image"

    if llm:
        try:
            prompt = (
                "You are an AI search query classifier. Classify the user query into exactly one intent:\n"
                "- 'image' (if user specifically wants an image, photo, chart, or diagram)\n"
                "- 'document' (if user wants factual text, rules, or information from documents)\n"
                "- 'both' (if user specifically asks for both text answers and relevant visual diagrams/images)\n\n"
                f"Query: \"{query}\"\n"
                "Output ONLY one word (image, document, or both): "
            )
            res = str(llm.complete(prompt)).strip().lower()
            if "image" in res and "both" not in res:
                return "image"
            elif "both" in res:
                return "both"
            else:
                return "document"
        except Exception:
            pass

    return "image" if has_image_kw else "document"
