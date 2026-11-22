import os
from typing import Any
from pydantic import Field, PrivateAttr
from dotenv import load_dotenv
import torch

from llama_index.core import Settings
from llama_index.core.llms.custom import CustomLLM
from llama_index.core.llms import LLMMetadata, CompletionResponse, CompletionResponseGen
from llama_index.core.llms.callbacks import llm_completion_callback
from llama_index.llms.openai_like import OpenAILike
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from neo4j import GraphDatabase

# Load environment variables
load_dotenv()


class VertexGenAILLM(CustomLLM):
    """Google GenAI / Vertex AI Enterprise LLM implementation for LlamaIndex."""
    model_name: str = Field(default="gemini-2.5-flash", description="The Gemini model name")
    project: str = Field(default="agent-506620", description="Google Cloud Project ID")
    location: str = Field(default="global", description="Vertex AI location")
    context_window: int = Field(default=1048576, description="Context window size")
    max_tokens: int = Field(default=8192, description="Max output tokens")
    temperature: float = Field(default=0.2, description="Sampling temperature")
    
    _client: Any = PrivateAttr()

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        from google import genai
        self._client = genai.Client(
            enterprise=True,
            project=self.project,
            location=self.location
        )

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(
            context_window=self.context_window,
            num_output=self.max_tokens,
            model_name=self.model_name,
            is_chat_model=True,
            is_function_calling_model=False,
        )

    @llm_completion_callback()
    def complete(self, prompt: str, **kwargs: Any) -> CompletionResponse:
        response = self._client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config={
                "temperature": self.temperature,
                "max_output_tokens": self.max_tokens,
            }
        )
        return CompletionResponse(text=response.text or "")

    @llm_completion_callback()
    def stream_complete(self, prompt: str, **kwargs: Any) -> CompletionResponseGen:
        response_stream = self._client.models.generate_content_stream(
            model=self.model_name,
            contents=prompt,
            config={
                "temperature": self.temperature,
                "max_output_tokens": self.max_tokens,
            }
        )
        accumulated_text = ""
        for chunk in response_stream:
            chunk_text = chunk.text or ""
            accumulated_text += chunk_text
            yield CompletionResponse(text=accumulated_text, delta=chunk_text)


def get_llm():
    """Initializes and returns the configured LLM (Google Vertex AI or OpenAI-compatible)."""
    provider = os.getenv("LLM_PROVIDER", "vertex").lower().strip()
    
    if provider in ["vertex", "vertexai", "gemini", "google"]:
        model_name = os.getenv("LLM_MODEL_NAME", "gemini-2.5-flash")
        project = os.getenv("VERTEX_PROJECT", "agent-506620")
        location = os.getenv("VERTEX_LOCATION", "global")
        context_window = int(os.getenv("LLM_CONTEXT_WINDOW", "1048576"))
        
        print(f"Initializing Vertex AI GenAI LLM (Model: {model_name}, Project: {project}, Location: {location})...")
        llm = VertexGenAILLM(
            model_name=model_name,
            project=project,
            location=location,
            context_window=context_window
        )
        return llm
    else:
        # Default OpenAI-compatible / Ollama / vLLM
        model_name = os.getenv("LLM_MODEL_NAME", "gemma4:12b")
        api_base = os.getenv("LLM_API_BASE", "http://localhost:11434/v1")
        api_key = os.getenv("LLM_API_KEY", "ollama")
        context_window = int(os.getenv("LLM_CONTEXT_WINDOW", "32768"))
        
        print(f"Initializing OpenAI-Compatible LLM (Model: {model_name}, API Base: {api_base})...")
        llm = OpenAILike(
            model=model_name,
            api_key=api_key,
            api_base=api_base,
            context_window=context_window,
            is_chat_model=True
        )
        return llm


def setup_global_settings():
    """Configures global settings for LlamaIndex."""
    llm = get_llm()
    Settings.llm = llm
    
    # Configure local embedding model (CUDA if available, else CPU)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    embed_model_name = os.getenv("EMBEDDING_MODEL_NAME", "BAAI/bge-small-en-v1.5")
    
    Settings.embed_model = HuggingFaceEmbedding(
        model_name=embed_model_name,
        device=device
    )
    
    return llm


def get_neo4j_driver():
    """Returns a Neo4j driver connection."""
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USERNAME", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "password")
    
    return GraphDatabase.driver(uri, auth=(user, password))
