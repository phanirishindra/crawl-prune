# URL → Full Site Markdown Extractor

## What it does
- Accepts a URL
- Fetches full HTML (static + dynamic fallback)
- Crawls internal pages (including pagination-like links)
- Prunes junk HTML
- Extracts core content as Markdown
- Preserves tables and code blocks where present
- Collects image/video/audio URLs
- Saves per-page markdown + combined markdown

## Install
```bash
pip install -r requirements.txt
playwright install chromium
```

## Run
```bash
python web_to_markdown.py "https://example.com/docs" --max-pages 50 --max-depth 2 --out output_markdown
```

## Outputs
- `output_markdown/combined.md`
- `output_markdown/manifest.json`
- `output_markdown/001_*.md`, `002_*.md`, ...

## Notes
- For very large sites, increase `--max-pages` carefully.
- Respect robots.txt and target site terms before crawling.
- LLM should be post-processing only, not responsible for crawling completeness.
