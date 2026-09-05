# 🎀 DocuAgent — Chat with Your Documents

> Upload any document and have a conversation with it — powered by **MCP**, **Gemini**, and **Streamlit**.

![Python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white)
![Streamlit](https://img.shields.io/badge/Streamlit-1.47%2B-FF4B4B?logo=streamlit&logoColor=white)
![Gemini](https://img.shields.io/badge/Gemini-3.6--flash-4285F4?logo=google&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

---

## ✨ Features

| Feature | Details |
|---|---|
| 🤖 **AI-powered chat** | Conversational Q&A backed by `gemini-3.6-flash` via the Agno agent framework |
| 🔗 **MCP filesystem** | Agent reads your file through `@modelcontextprotocol/server-filesystem`; gracefully falls back if Node.js is unavailable |
| 📄 **PDF text extraction** | Uses `pypdf` to extract real text from PDFs (not raw bytes) before sending to the model |
| 📁 **Broad file support** | `.txt` · `.pdf` · `.md` · `.csv` · `.json` · `.yaml` · `.yml` · `.docx` |
| 🔑 **Smart key management** | Uses your `.env` key by default; prompts for a personal key if quota is hit |
| ⏳ **Auto-retry on rate limits** | Parses the `retryDelay` from 429 responses and retries automatically |
| 🧹 **Session-scoped storage** | Files live only in a temp dir tied to your session — nothing is persisted |
| 💡 **Example questions** | One-click prompts in the sidebar to get started instantly |

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+ *(optional — app works without it, see [Architecture](#️-architecture) below)*
- A free [Google AI Studio API key](https://aistudio.google.com)

### Installation

```bash
# 1. Clone the repo
git clone https://github.com/annshita/DocuAgent.git
cd DocuAgent

# 2. Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure your API key
cp .env.example .env
# Open .env and paste your Google AI Studio key
```

### Run

```bash
streamlit run main.py
```

Open **http://localhost:8501**, upload a document from the sidebar, and start chatting.

---

## ⚙️ Configuration

Create a `.env` file (copy from `.env.example`):

```env
GOOGLE_API_KEY=your_google_ai_studio_key_here
```

| Variable | Required | Description |
|---|---|---|
| `GOOGLE_API_KEY` | Yes (or enter in UI) | [Get a free key at aistudio.google.com](https://aistudio.google.com) |

If the key is set, the sidebar shows **"Built-in key active"**. If the free-tier quota is hit, the app surfaces a key input so you can enter a personal key without restarting.

---

## 🏗️ Architecture

### Agent execution flow

```
User browser
    │  HTTP
    ▼
Streamlit UI  (main.py)
    │
    ├─ _get_safe_file_content()
    │    ├─ PDF  → pypdf text extraction (capped at ~30K tokens)
    │    └─ Text → UTF-8 / latin-1 decode
    │
    └─ asyncio.run( run_mcp_agent() )
            │
            ├── npx found on PATH?
            │       │
            │      YES → MCP stdio server → Agent WITH read_file tool
            │       │
            │      NO / connection fails
            │           ↓
            └──────── Agent WITHOUT tools
                      (file content already in system prompt — works fine)
```

### Token-budget strategy

The file text is pre-loaded locally and injected directly into the system prompt **(capped at 120,000 chars ≈ 30,000 tokens)** before the agent runs. This means:

- No mandatory MCP round-trip — the agent can answer immediately from the prompt
- Stays under the free-tier 250,000 input-token-per-minute limit
- MCP `read_file` is a last-resort fallback only, not the primary path
- The app works on platforms where `npx` / Node.js is unavailable

---

## 🤖 Models

| Role | Model | Notes |
|---|---|---|
| Primary | `gemini-3.6-flash` | Used for all requests |
| Fallback | `gemini-1.5-flash` | Used automatically if primary returns 503/overloaded |

---

## ☁️ Deploy to Streamlit Community Cloud

1. Push this repo to GitHub (make sure `.env` is in `.gitignore` ✅)
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**
3. Point it at `main.py`
4. Add your key under **Settings → Secrets**:
   ```toml
   GOOGLE_API_KEY = "your_key_here"
   ```
5. Click **Deploy**

> **Node.js:** `packages.txt` instructs Streamlit Cloud to install `nodejs` and `npm`. If the MCP server still can't start, the app automatically falls back to running the agent without tools — so it works either way.

> **OS detection:** The app auto-selects `npx.cmd` (Windows) or `npx` (Linux/macOS) at runtime.

---

## 📂 Project Structure

```
DocuAgent/
├── main.py           # Streamlit app + Agno MCP agent logic
├── styles.css        # Custom pink/rose theme
├── requirements.txt  # pip dependencies
├── pyproject.toml    # Project metadata
├── packages.txt      # System packages for Streamlit Cloud (nodejs, npm)
├── .env.example      # API key template
└── .gitignore
```

---

## 📋 File Upload Policy

- Files are written to a `tempfile.mkdtemp()` directory scoped to the Streamlit session
- Uploading a **new** file immediately clears the previous one
- Session end triggers cleanup via `weakref.finalize` (best-effort)
- **Maximum upload size: 10 MB**
- Scanned / image-only PDFs without a text layer are detected and the user is notified

---

## 🪪 License

MIT
