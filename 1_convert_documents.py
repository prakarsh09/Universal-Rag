import os
import subprocess
from pathlib import Path
import pypdfium2
from pdftext.extraction import paginated_plain_text_output

INPUT_DIR = "input_docs"
OUTPUT_DIR = "markdown_output"

def extract_via_vision_llm(pdf_path: Path, md_file_path: Path, page_count: int) -> bool:
    """Extracts text, tables, and details from scanned or image PDFs using Gemini 2.5 Flash."""
    try:
        from google import genai
        project = os.getenv("VERTEX_PROJECT", "agent-506620")
        location = os.getenv("VERTEX_LOCATION", "global")
        model_name = os.getenv("LLM_MODEL_NAME", "gemini-2.5-flash")
        client = genai.Client(vertexai=True, project=project, location=location)
        
        print(f"Running Gemini Multimodal Vision OCR ({page_count} pages)...")
        pdf_doc = pypdfium2.PdfDocument(str(pdf_path))
        page_sections = []
        for idx, page in enumerate(pdf_doc, start=1):
            img = page.render(scale=2).to_pil()
            prompt = (
                "Transcribe all text, tables, numbers, and structural details from this document page into clear, well-structured Markdown. "
                "Preserve all headings, tables, labels, and language scripts (e.g. Hindi, English) accurately. "
                "Output only the transcribed markdown content without conversational commentary."
            )
            res = client.models.generate_content(model=model_name, contents=[img, prompt])
            txt = (res.text or "").strip()
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
            print(f"Successfully transcribed {pdf_path.name} via Gemini Vision OCR ({len(full_md)} chars).")
            return True
    except Exception as e:
        print(f"Gemini Vision OCR error: {e}")
    return False

def convert_single_pdf(pdf_path: Path, target_out_dir: Path):
    """Converts a single PDF to Markdown with automatic memory safeguards, text checks, and vision fallback."""
    filename = pdf_path.stem
    md_file_path = target_out_dir / f"{filename}.md"
    
    # Check page count
    try:
        pdf_doc = pypdfium2.PdfDocument(str(pdf_path))
        page_count = len(pdf_doc)
        pdf_doc.close()
    except Exception:
        page_count = 1

    # Check if digital text exists
    is_digital = False
    try:
        sample_pages = paginated_plain_text_output(str(pdf_path), page_range=range(min(3, page_count)), sort=True)
        is_digital = sum(len(p.strip()) for p in sample_pages) > 100
    except Exception:
        pass

    # Large digital document (> 25 pages) -> fast streaming extraction to prevent Marker uncompressed bitmap MemoryError
    if page_count > 25 and is_digital:
        print(f"Document has {page_count} digital pages. Using high-speed streaming extractor to prevent MemoryError...")
        pages = paginated_plain_text_output(str(pdf_path), sort=True)
        md_sections = [f"## Page {idx}\n\n{page.strip()}" for idx, page in enumerate(pages, 1) if page.strip()]
        with open(md_file_path, "w", encoding="utf-8") as f:
            f.write("\n\n".join(md_sections))
        print(f"Successfully converted {filename} ({page_count} pages) to Markdown.")
        return

    # Standard document (<= 25 pages): Run Marker
    try:
        env = {**os.environ, "TORCH_DEVICE": "cuda", "CUDA_VISIBLE_DEVICES": "0", "USE_GPU": "1", "PYTHONIOENCODING": "utf-8"}
        subprocess.run(
            ["marker_single", str(pdf_path), "--output_dir", str(target_out_dir), "--output_format", "markdown", "--disable_image_extraction"],
            env=env,
            check=True
        )
        # Verify the output is non-empty
        possible_mds = list(target_out_dir.glob("*.md")) + list(target_out_dir.rglob("*.md"))
        if possible_mds and possible_mds[0].stat().st_size > 30:
            print(f"Successfully converted {filename} with Marker.")
            return
        else:
            print(f"Marker generated an empty output for {filename}. Attempting fallbacks...")
    except Exception as e:
        print(f"Marker layout conversion failed ({e}). Attempting text fallback...")

    # Fallback 1: pdftext
    try:
        pages = paginated_plain_text_output(str(pdf_path), sort=True)
        md_sections = [f"## Page {idx}\n\n{page.strip()}" for idx, page in enumerate(pages, 1) if page.strip()]
        if md_sections:
            full_text = "\n\n".join(md_sections)
            if len(full_text.strip()) > 30:
                with open(md_file_path, "w", encoding="utf-8") as f:
                    f.write(full_text)
                print(f"Successfully converted {filename} via fallback extractor.")
                return
    except Exception as fb_err:
        print(f"Fallback extraction failed: {fb_err}")

    # Fallback 2: Multimodal Vision OCR (Gemini 2.5 Flash)
    if extract_via_vision_llm(pdf_path, md_file_path, page_count):
        return

    print(f"Error: All extraction methods failed for {filename}.")

def convert_all_pdfs():
    """Converts all PDFs in the input directory to Markdown using marker layout parser."""
    os.makedirs(INPUT_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    input_path = Path(INPUT_DIR)
    pdf_files = list(input_path.rglob("*.pdf"))
    
    if not pdf_files:
        print(f"No PDF files found in '{INPUT_DIR}'. Please place your PDF documents in '{INPUT_DIR}' (supports subdirectories).")
        return

    print(f"Found {len(pdf_files)} PDF files. Starting conversion...")

    for pdf_path in pdf_files:
        rel_dir = pdf_path.parent.relative_to(input_path)
        target_out_dir = Path(OUTPUT_DIR) / rel_dir
        os.makedirs(target_out_dir, exist_ok=True)
        
        filename = pdf_path.stem
        print(f"Converting {filename} from '{rel_dir}'...")
        convert_single_pdf(pdf_path, target_out_dir)

if __name__ == "__main__":
    convert_all_pdfs()
