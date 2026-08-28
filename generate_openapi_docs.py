#!/usr/bin/env python3
"""Standalone OpenAPI 3.0 & Redoc Developer Documentation Generator for RegEngine AI.

Generates:
  1. `docs/openapi.json` — Complete OpenAPI 3.0.3 schema specification JSON.
  2. `docs/redoc.html` — Standalone, offline-capable Redoc documentation bundle with
     custom enterprise dark sidebar theme, sticky navigation, and 5 domain sections.

Usage:
  python generate_openapi_docs.py --output-dir docs
"""

import argparse
import json
import logging
import os
import sys

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app.main import app
from app.openapi import get_custom_openapi

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("generate_openapi_docs")


REDOC_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>RegEngine AI — API Documentation & Developer Portal</title>
    <link rel="shortcut icon" href="https://fastapi.tiangolo.com/img/favicon.png" />
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
      body {{
        margin: 0;
        padding: 0;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      }}
    </style>
  </head>
  <body>
    <div id="redoc-container"></div>
    <script src="https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js"></script>
    <script>
      const spec = {spec_json_str};

      Redoc.init(
        spec,
        {{
          scrollYOffset: 0,
          hideDownloadButton: false,
          expandResponses: "200,201",
          requiredPropsFirst: true,
          noAutoAuth: false,
          pathInMiddlePanel: true,
          theme: {{
            colors: {{
              primary: {{
                main: "#6366f1"
              }},
              success: {{
                main: "#10b981"
              }},
              warning: {{
                main: "#f59e0b"
              }},
              error: {{
                main: "#ef4444"
              }},
              text: {{
                primary: "#0f172a",
                secondary: "#475569"
              }}
            }},
            typography: {{
              fontFamily: "'Inter', sans-serif",
              headings: {{
                fontFamily: "'Inter', sans-serif",
                fontWeight: "700"
              }},
              code: {{
                fontFamily: "'JetBrains Mono', monospace",
                fontSize: "13px"
              }}
            }},
            sidebar: {{
              backgroundColor: "#0f172a",
              textColor: "#f8fafc",
              activeTextColor: "#818cf8",
              groupItems: {{
                textTransform: "uppercase"
              }}
            }},
            rightPanel: {{
              backgroundColor: "#1e293b",
              textColor: "#ffffff"
            }}
          }}
        }},
        document.getElementById("redoc-container")
      );
    </script>
  </body>
</html>
"""


def generate_docs(output_dir: str, json_filename: str, html_filename: str) -> None:
    os.makedirs(output_dir, exist_ok=True)

    logger.info("Generating customized OpenAPI 3.0.3 schema from FastAPI app...")
    schema = get_custom_openapi(app)

    json_path = os.path.join(output_dir, json_filename)
    html_path = os.path.join(output_dir, html_filename)

    # 1. Write openapi.json
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2)
    logger.info("✅ Exported OpenAPI JSON schema: %s", json_path)

    # 2. Write redoc.html
    spec_json_str = json.dumps(schema)
    html_content = REDOC_HTML_TEMPLATE.format(spec_json_str=spec_json_str)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    logger.info("✅ Exported Standalone Redoc HTML Bundle: %s", html_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate OpenAPI 3.0 & Redoc Documentation")
    parser.add_argument("--output-dir", default="docs", help="Target output directory for documentation artifacts")
    parser.add_argument("--json-filename", default="openapi.json", help="Output OpenAPI JSON filename")
    parser.add_argument("--html-filename", default="redoc.html", help="Output Redoc HTML filename")

    args = parser.parse_args()
    generate_docs(args.output_dir, args.json_filename, args.html_filename)


if __name__ == "__main__":
    main()
