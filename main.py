# Standard library imports
import os
import re
import sys
import asyncio
import json
import weakref
import logging
import tempfile
import shutil

logging.basicConfig(level=logging.INFO)

# Third-party imports
import streamlit as st
from dotenv import load_dotenv

# PDF extraction
try:
    from pypdf import PdfReader
    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False

# Local imports
from agno.models.google import Gemini
from agno.tools.mcp import MCPTools
from agno.agent import Agent, RunOutput
from textwrap import dedent
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Load environment variables
load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_UPLOAD_BYTES   = 10 * 1024 * 1024          # 10 MB hard limit
ALLOWED_EXTENSIONS = ["txt", "pdf", "md", "csv", "json", "yaml", "yml", "docx"]
PRIMARY_MODEL      = "gemini-3.6-flash"        # latest stable model
FALLBACK_MODEL     = "gemini-1.5-flash"

# Free-tier input token limit is 250 000 tokens/min.
# ~4 chars ≈ 1 token → keep each request under ~150 000 chars (~37 500 tokens)
# to leave headroom for prompt + history.
MAX_CHARS_PER_REQUEST = 120_000                # ≈30 000 tokens
MAX_RETRY_WAIT_S      = 65                     # max seconds to wait on 429

# ---------------------------------------------------------------------------
# CSS loader
# ---------------------------------------------------------------------------
def load_css(path: str) -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Temp-dir helpers
# ---------------------------------------------------------------------------
def _make_temp_dir() -> str:
    """Create a session-scoped temp directory."""
    return tempfile.mkdtemp(prefix="docagent_")


def _cleanup_dir(path: str) -> None:
    """Best-effort removal of a temp dir."""
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def _clear_dir(path: str) -> None:
    """Remove all files inside *path* without removing the directory itself."""
    for name in os.listdir(path):
        fp = os.path.join(path, name)
        try:
            if os.path.isfile(fp) or os.path.islink(fp):
                os.unlink(fp)
            elif os.path.isdir(fp):
                shutil.rmtree(fp)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Error classification helpers
# ---------------------------------------------------------------------------
def _classify_error(text: str) -> str:
    """
    Inspect an error string (or JSON blob) and return:
      'rate_limit'  - 429 per-minute RPM/token limit (wait and retry)
      'quota'       - Monthly/daily quota fully exhausted (need a new key)
      'overloaded'  - 503 / high demand
      'other'       - anything else
    """
    lo = text.lower()
    # RESOURCE_EXHAUSTED covers both per-minute token limits and monthly caps.
    # If there is a retryDelay the API says it is recoverable → rate_limit.
    if "resource_exhausted" in lo or "quota_exceeded" in lo or "quota exceeded" in lo:
        if any(k in lo for k in ("retrydelay", "retry in", "per minute", "per_minute",
                                  "rpm", "input_token", "inputtoken")):
            return "rate_limit"
        return "quota"
    if any(k in lo for k in ("429", "rate limit", "rate_limit", "exhausted")):
        return "rate_limit"
    if any(k in lo for k in ("503", "high demand", "overloaded", "unavailable")):
        return "overloaded"
    return "other"


def _parse_retry_delay(text: str) -> float:
    """Extract the retry delay (seconds) from an API error message, or return MAX_RETRY_WAIT_S."""
    # API returns either 'retryDelay: "31s"' or 'Retry in 31.4s'
    match = re.search(r'retry(?:delay|\s+in)[\s":]*([\d.]+)\s*s', text, re.IGNORECASE)
    if match:
        return min(float(match.group(1)) + 2, MAX_RETRY_WAIT_S)  # +2s safety margin
    return 35.0  # safe default


def _get_content(response: RunOutput) -> str:
    """Extract the best available text from a RunOutput."""
    content = ""
    if hasattr(response, "get_content_as_string"):
        content = response.get_content_as_string() or ""
    if not content and response.content:
        content = str(response.content)
    return content.strip()


def _is_error_response(response: RunOutput) -> bool:
    """Return True if the RunOutput represents a model error (not just slow)."""
    from agno.run.agent import RunStatus  # type: ignore
    return response.status == RunStatus.error


# ---------------------------------------------------------------------------
# File content extraction
# ---------------------------------------------------------------------------
def _extract_pdf_text(filepath: str) -> tuple[str, bool]:
    """Extract text from a PDF using pypdf. Returns (text, was_truncated)."""
    if not _PYPDF_AVAILABLE:
        return (
            "(pypdf is not installed — cannot extract PDF text. "
            "Run: pip install pypdf)",
            False,
        )
    try:
        reader = PdfReader(filepath)
        pages_text: list[str] = []
        total_chars = 0
        truncated = False
        for page in reader.pages:
            page_text = page.extract_text() or ""
            if total_chars + len(page_text) > MAX_CHARS_PER_REQUEST:
                # Take as much of this page as fits
                remaining = MAX_CHARS_PER_REQUEST - total_chars
                if remaining > 0:
                    pages_text.append(page_text[:remaining])
                truncated = True
                break
            pages_text.append(page_text)
            total_chars += len(page_text)
        text = "\n\n".join(pages_text).strip()
        if not text:
            return (
                "(PDF has no extractable text layer — it may be a scanned image PDF. "
                "Please upload a text-based PDF or convert it first.)",
                False,
            )
        return text, truncated
    except Exception as e:
        return f"(PDF extraction failed: {e})", False


def _get_safe_file_content(filepath: str) -> tuple[str, bool]:
    """
    Extract readable text from any supported file type.
    Returns (content, was_truncated).
    - PDFs  → extracted via pypdf (proper text layer extraction)
    - Other → decoded as UTF-8 / latin-1 and capped at MAX_CHARS_PER_REQUEST
    """
    ext = os.path.splitext(filepath)[1].lower()

    if ext == ".pdf":
        return _extract_pdf_text(filepath)

    # Plain-text files
    try:
        with open(filepath, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        return f"(Could not read file: {e})", False

    for enc in ("utf-8", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return (
            "(The file appears to be binary and could not be decoded. "
            "Please upload a text-based file.)",
            False,
        )

    if len(text) <= MAX_CHARS_PER_REQUEST:
        return text, False
    return text[:MAX_CHARS_PER_REQUEST], True


# ---------------------------------------------------------------------------
# MCP agent
# ---------------------------------------------------------------------------


async def run_mcp_agent(local_dir: str, query: str, api_key: str, filename: str = "") -> tuple[str, bool]:
    """
    Run the MCP filesystem agent.

    Strategy to stay within free-tier limits:
    - Pre-read the file locally and inject a safe-sized excerpt into the
      system prompt instead of relying on the MCP `read_file` tool for
      the full content. This caps the input tokens per request.
    - On a 429 rate-limit response, wait for the API-specified retry delay
      and retry once automatically.

    Args:
        local_dir:  Temp directory the MCP server is rooted at.
        query:      User's question.
        api_key:    Google AI Studio API key.
        filename:   Name of the uploaded file (so the agent reads it directly).

    Returns:
        (response_text, quota_exceeded)
    """
    abs_dir = os.path.abspath(local_dir)
    file_hint = f"/{filename}" if filename else ""

    # Pre-load file content locally so we can cap token usage
    file_content = ""
    truncation_note = ""
    if filename:
        filepath = os.path.join(abs_dir, filename)
        if os.path.isfile(filepath):
            file_content, was_truncated = _get_safe_file_content(filepath)
            if was_truncated:
                truncation_note = (
                    f"\n\n> **Note:** The document is large; showing the first "
                    f"{MAX_CHARS_PER_REQUEST // 1000}K characters. "
                    "If your question targets content later in the document, "
                    "please be more specific or split the file."
                )



async def _execute_agent_logic(
    query: str,
    api_key: str,
    instructions: str,
    truncation_note: str,
    tools: list | None = None,
) -> tuple[str, bool]:
    """
    Core agent execution with retry logic.
    Shared by both the MCP path (tools=[mcp_tools]) and the
    no-MCP fallback path (tools=None / tools=[]).
    """

    def _make_agent(model_id: str) -> Agent:
        kwargs = dict(
            model=Gemini(id=model_id, api_key=api_key),
            instructions=instructions,
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_call_limit"] = 1
        return Agent(**kwargs)

    async def _run_model(model_id: str) -> tuple[str | None, str, str]:
        response = await _make_agent(model_id).arun(query)
        if _is_error_response(response):
            err_content = _get_content(response)
            return None, _classify_error(err_content), err_content
        content = _get_content(response)
        return content or "(No text returned — try rephrasing your question.)", "", ""

    async def _run_with_retry(model_id: str) -> tuple[str | None, str, str]:
        content, err, raw = await _run_model(model_id)
        if err == "rate_limit":
            wait_s = _parse_retry_delay(raw)
            logging.info("Rate limit hit — auto-retrying in %.0f s …", wait_s)
            await asyncio.sleep(wait_s)
            content, err, raw = await _run_model(model_id)
        return content, err, raw

    content, err, raw_err = await _run_with_retry(PRIMARY_MODEL)

    if not err:
        return (content or "") + truncation_note, False

    if err == "rate_limit":
        return (
            "> ⏳ **Rate Limit Hit**  \n"
            "> The API key hit its **per-minute token limit** (free tier).  \n"
            "> The app already waited and retried once automatically.  \n"
            "> **Please wait ~60 s and try again** — or enter your own "
            "Google AI Studio key in the sidebar to bypass this limit."
        ), True

    if err == "quota":
        return (
            "> ⚠️ **Quota Exhausted**  \n"
            "> The built-in API key has exhausted its daily/monthly quota. "
            "Please enter your own Google AI Studio key in the sidebar."
        ), True

    if err == "overloaded":
        fb_content, fb_err, _ = await _run_with_retry(FALLBACK_MODEL)
        if not fb_err:
            note = (
                f"\n\n*(Note: Answer from fallback model "
                f"`{FALLBACK_MODEL}` — primary model was overloaded)*"
            )
            return (fb_content or "") + note + truncation_note, False
        if fb_err in ("quota", "rate_limit"):
            return (
                "> ⚠️ **Quota / Rate Limit**  \n"
                "> The built-in API key has hit its limit. "
                "Please enter your own Google AI Studio key in the sidebar."
            ), True
        return (
            "> ⚠️ **High Demand**  \n"
            "> Both models are overloaded. Please try again in a few seconds."
        ), False

    return "Agent error — please try again or enter your own API key.", False


async def run_mcp_agent(local_dir: str, query: str, api_key: str, filename: str = "") -> tuple[str, bool]:
    """
    Run the document Q&A agent.

    Primary path:  MCP stdio server via npx (gives agent a read_file fallback).
    Fallback path: No MCP tools — works fine because file content is already
                   injected into the system prompt, so npx is not required.
                   This makes the app work on Streamlit Cloud even if Node.js
                   is unavailable.
    """
    abs_dir = os.path.abspath(local_dir)
    file_hint = f"/{filename}" if filename else ""

    # Pre-load file content so we can cap token usage
    file_content = ""
    truncation_note = ""
    if filename:
        filepath = os.path.join(abs_dir, filename)
        if os.path.isfile(filepath):
            file_content, was_truncated = _get_safe_file_content(filepath)
            if was_truncated:
                truncation_note = (
                    f"\n\n> **Note:** The document is large; showing the first "
                    f"{MAX_CHARS_PER_REQUEST // 1000}K characters. "
                    "If your question targets content later in the document, "
                    "please be more specific or split the file."
                )

    instructions = dedent(f"""
        You are a precise document assistant.
        The user has uploaded a file. Its path on the MCP filesystem is: {file_hint}

        The file content (or a safe excerpt) has been pre-loaded below.
        Use it to answer the user's question WITHOUT calling any tools unless
        the content below is missing or incomplete.

        --- BEGIN FILE CONTENT ---
        {file_content if file_content else "(No content pre-loaded — use read_file tool to read the file.)"}
        --- END FILE CONTENT ---

        Rules:
        - Answer concisely and use markdown formatting where helpful.
        - Never output raw file content verbatim; always process and summarise.
        - If and only if the pre-loaded content is insufficient or empty,
          use the `read_file` tool ONCE to fetch the file.
    """)

    # ── Primary path: try to connect to the MCP filesystem server ────────────
    npx_cmd = "npx.cmd" if sys.platform == "win32" else "npx"

    # Quick check: is npx actually on PATH?
    npx_available = shutil.which(npx_cmd) is not None

    if npx_available:
        env = os.environ.copy()
        env.update({
            "npm_config_update_notifier": "false",
            "npm_config_loglevel": "error",
        })
        server_params = StdioServerParameters(
            command=npx_cmd,
            args=["-y", "--silent", "@modelcontextprotocol/server-filesystem", abs_dir],
            env=env,
        )
        try:
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    mcp_tools = MCPTools(session=session)
                    await mcp_tools.initialize()
                    # Restrict to read_file only
                    mcp_tools.functions = {
                        name: fn
                        for name, fn in mcp_tools.functions.items()
                        if name in {"read_file"}
                    }
                    return await _execute_agent_logic(
                        query, api_key, instructions, truncation_note,
                        tools=[mcp_tools],
                    )
        except Exception as mcp_err:
            logging.warning(
                "MCP connection failed (%s) — falling back to no-tools mode.", mcp_err
            )
            # Fall through to the no-tools path below

    # ── Fallback path: no MCP tools (content already in system prompt) ────────
    logging.info(
        "Running agent without MCP tools (npx %s).",
        "not found" if not npx_available else "connection failed",
    )
    # Remove the read_file instruction since there are no tools available
    instructions_no_tools = instructions.replace(
        "- If and only if the pre-loaded content is insufficient or empty,\n"
        "          use the `read_file` tool ONCE to fetch the file.",
        "- If the pre-loaded content seems incomplete, let the user know and\n"
        "          ask them to try a more specific question.",
    )
    return await _execute_agent_logic(
        query, api_key, instructions_no_tools, truncation_note, tools=None
    )


# ---------------------------------------------------------------------------
# Streamlit App
# ---------------------------------------------------------------------------
st.set_page_config(page_title="DocuAgent", page_icon="🎀", layout="wide")
load_css("styles.css")

# Session-state bootstrap
if "messages" not in st.session_state:
    st.session_state.messages = []

if "temp_dir" not in st.session_state:
    td = _make_temp_dir()
    st.session_state.temp_dir = td
    # Register cleanup via weakref so Python GC removes the dir when the
    # session object is eventually collected (best-effort on hosted platforms).
    weakref.finalize(st.session_state, _cleanup_dir, td)

if "show_key_input" not in st.session_state:
    st.session_state.show_key_input = False

if "uploaded_filename" not in st.session_state:
    st.session_state.uploaded_filename = None

# Read env key once (hidden from UI unless needed)
_env_api_key = os.getenv("GOOGLE_API_KEY", "").strip()

# ---------------------------------------------------------------------------
# Header row
# ---------------------------------------------------------------------------
col_title, col_btn = st.columns([5, 1])
with col_title:
    st.markdown(
        '<h1 class="app-title">🎀 DocuAgent</h1>'
        '<p class="app-subtitle">Upload a document and chat with it using MCP + Gemini 💕</p>',
        unsafe_allow_html=True,
    )
with col_btn:
    st.write("")
    st.write("")
    if st.button("🗑️ Clear Chat", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🔑 API Key")

    # Show built-in key status
    if _env_api_key:
        st.markdown(
            '<span class="key-badge active">✓ Built-in key active</span>',
            unsafe_allow_html=True,
        )

    # Show a warning banner when a limit was hit
    if st.session_state.show_key_input:
        st.warning(
            "API limit reached on the built-in key. "
            "Enter your own key below to continue — or wait ~60 s if it's a per-minute limit.",
            icon="⚠️",
        )

    # Key input is ALWAYS visible so users can override at any time
    ui_api_key = st.text_input(
        "Your Google AI Studio API Key (optional)",
        type="password",
        placeholder="AIza… (leave blank to use built-in key)",
        help="Get a free key at aistudio.google.com",
        key="user_api_key_input",
    )

    if ui_api_key.strip():
        st.markdown(
            '<span class="key-badge user">✓ Using your key</span>',
            unsafe_allow_html=True,
        )

    # User-provided key takes priority over the built-in env key
    effective_key = ui_api_key.strip() if ui_api_key.strip() else _env_api_key

    # Mirror into os.environ so the Gemini SDK always picks up the right key
    if effective_key:
        os.environ["GOOGLE_API_KEY"] = effective_key

    st.markdown("---")

    uploaded_file = st.file_uploader(
        "📄 Your Document",
        type=ALLOWED_EXTENSIONS,
        help=f"Supported: {', '.join(ALLOWED_EXTENSIONS)}. Max 10 MB.",
    )

    if uploaded_file is not None:
        file_bytes = uploaded_file.getbuffer()
        if len(file_bytes) > MAX_UPLOAD_BYTES:
            st.error(
                f"File too large ({len(file_bytes) / 1_048_576:.1f} MB). "
                "Please upload a file under 10 MB."
            )
        else:
            if uploaded_file.name != st.session_state.uploaded_filename:
                _clear_dir(st.session_state.temp_dir)
                dest = os.path.join(st.session_state.temp_dir, uploaded_file.name)
                with open(dest, "wb") as fh:
                    fh.write(file_bytes)
                st.session_state.uploaded_filename = uploaded_file.name
                st.session_state.messages = []  # fresh chat for new doc
            st.success(f"✅ **{uploaded_file.name}** ready")

    st.markdown("---")
    st.markdown("### 💡 Example Questions")
    examples = [
        "Summarise this document.",
        "What is the main topic?",
        "Extract the key points.",
        "List any action items or conclusions.",
    ]
    for q in examples:
        if st.button(q, key=f"ex_{hash(q)}", use_container_width=True):
            st.session_state.messages.append({"role": "user", "content": q})
            st.rerun()

    st.markdown("---")
    st.caption("Files are stored in a temporary directory and deleted when the session ends.")

# ---------------------------------------------------------------------------
# Chat history display
# ---------------------------------------------------------------------------
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# Detect pending prompt from example-button clicks
pending_prompt: str | None = None
if st.session_state.messages:
    last = st.session_state.messages[-1]
    if last["role"] == "user":
        if (
            len(st.session_state.messages) == 1
            or st.session_state.messages[-2]["role"] != "assistant"
        ):
            pending_prompt = last["content"]

# ---------------------------------------------------------------------------
# Chat input
# ---------------------------------------------------------------------------
typed_prompt = st.chat_input("Ask a question about your document...")
prompt = typed_prompt or pending_prompt

# ---------------------------------------------------------------------------
# Handle prompt
# ---------------------------------------------------------------------------
if prompt:
    if not effective_key:
        st.error("Please enter your Google AI Studio API key in the sidebar.")
        st.stop()

    if not st.session_state.uploaded_filename:
        st.error("Please upload a document first (sidebar → Upload Document).")
        st.stop()

    # Add user message (avoid duplicating from example-button path)
    if not (
        st.session_state.messages
        and st.session_state.messages[-1]["role"] == "user"
        and st.session_state.messages[-1]["content"] == prompt
    ):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

    # Get agent response
    with st.chat_message("assistant"):
        status_box = st.empty()
        status_box.info(
            "🌸 Reading your document and thinking… "
            "(large PDFs may take up to 30 seconds)"
        )
        try:
            response_text, quota_hit = asyncio.run(
                run_mcp_agent(
                    st.session_state.temp_dir,
                    prompt,
                    effective_key,
                    filename=st.session_state.uploaded_filename or "",
                )
            )
        except Exception as run_err:
            response_text = f"Unexpected error: {run_err}"
            quota_hit = False
        status_box.empty()

        if quota_hit:
            st.session_state.show_key_input = True

        if response_text:
            st.markdown(response_text)
            st.session_state.messages.append(
                {"role": "assistant", "content": response_text}
            )
        else:
            msg = "No response received from the agent."
            st.error(msg)
            st.session_state.messages.append({"role": "assistant", "content": msg})

    if quota_hit:
        st.rerun()  # re-render sidebar so key input appears immediately
