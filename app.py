import base64
import calendar
import io
import json
import os
import re
import textwrap
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st
from PIL import Image
from supabase import create_client, Client

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

APP_NAME = "Homework Hub"
DEFAULT_MODEL = "gpt-4.1-mini"
BUCKET_NAME = "homework-docs"


# -----------------------------
# Page setup and mobile styling
# -----------------------------
st.set_page_config(
    page_title=APP_NAME,
    page_icon="📚",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
      .block-container {
        padding-top: 1rem;
        padding-bottom: 3rem;
        max-width: 860px;
      }
      .big-title {
        font-size: 2.1rem;
        line-height: 1.05;
        font-weight: 800;
        margin-bottom: .1rem;
      }
      .subtitle {
        color: #667085;
        margin-bottom: 1rem;
      }
      .metric-card {
        border: 1px solid #EAECF0;
        border-radius: 18px;
        padding: 1rem;
        background: #FFFFFF;
        box-shadow: 0 1px 2px rgba(16,24,40,.05);
        margin-bottom: .75rem;
      }
      .assignment-card {
        border: 1px solid #EAECF0;
        border-radius: 18px;
        padding: .95rem 1rem;
        background: #FFFFFF;
        margin-bottom: .7rem;
      }
      .small-muted { color: #667085; font-size: .9rem; }
      .due-today { color: #B42318; font-weight: 700; }
      .due-soon { color: #B54708; font-weight: 700; }
      .done { color: #027A48; font-weight: 700; }
      div[data-testid="stRadio"] label { font-size: .95rem; }
      .stButton>button {
        width: 100%;
        border-radius: 14px;
        padding: .8rem .9rem;
        font-weight: 700;
      }
      @media (max-width: 640px) {
        .block-container { padding-left: .9rem; padding-right: .9rem; }
        .big-title { font-size: 1.8rem; }
      }
    </style>
    """,
    unsafe_allow_html=True,
)


# -----------------------------
# Supabase connection
# -----------------------------
@st.cache_resource
def get_supabase() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)


def upload_image_to_storage(uploaded_file: Any, folder: str = "assignments") -> Optional[str]:
    """Upload image to Supabase Storage and return the path."""
    try:
        supabase = get_supabase()
        file_bytes = uploaded_file.getvalue()
        ext = getattr(uploaded_file, "type", "image/jpeg").split("/")[-1]
        if ext not in ["jpeg", "jpg", "png", "webp"]:
            ext = "jpg"
        filename = f"{folder}/{uuid.uuid4()}.{ext}"

        supabase.storage.from_(BUCKET_NAME).upload(
            path=filename,
            file=file_bytes,
            file_options={"content-type": getattr(uploaded_file, "type", "image/jpeg"), "upsert": "true"},
        )
        return filename
    except Exception as e:
        st.warning(f"Could not save image: {e}")
        return None


def get_image_url(image_path: Optional[str]) -> Optional[str]:
    """Get a temporary signed URL for a private image."""
    if not image_path:
        return None
    try:
        supabase = get_supabase()
        res = supabase.storage.from_(BUCKET_NAME).create_signed_url(image_path, 3600)  # 1 hour
        return res.get("signedURL") or res.get("signedUrl")
    except Exception:
        return None


def display_stored_image(image_path: Optional[str], caption: str = "Original photo") -> None:
    url = get_image_url(image_path)
    if url:
        st.image(url, caption=caption, use_container_width=True)
    else:
        st.caption("Original photo not available.")


# -----------------------------
# Database helpers (Supabase)
# -----------------------------
def add_assignment(record: Dict[str, Any]) -> str:
    supabase = get_supabase()
    data = {
        "class_name": record.get("class_name"),
        "title": record.get("title") or "Untitled assignment",
        "description": record.get("description"),
        "due_date": record.get("due_date"),
        "due_time": record.get("due_time"),
        "assignment_type": record.get("assignment_type"),
        "estimated_effort_minutes": safe_int(record.get("estimated_effort_minutes")),
        "priority": record.get("priority") or "Normal",
        "status": record.get("status") or "Not started",
        "source": record.get("source"),
        "uncertainty_notes": record.get("uncertainty_notes"),
        "image_path": record.get("image_path"),
    }
    res = supabase.table("assignments").insert(data).execute()
    return res.data[0]["id"]


def update_assignment_status(assignment_id: str, status: str) -> None:
    supabase = get_supabase()
    supabase.table("assignments").update({"status": status}).eq("id", assignment_id).execute()


def delete_assignment(assignment_id: str) -> None:
    supabase = get_supabase()
    # Optionally also delete the image from storage here
    supabase.table("assignments").delete().eq("id", assignment_id).execute()


def add_study_material(record: Dict[str, Any]) -> str:
    supabase = get_supabase()
    data = {
        "class_name": record.get("class_name"),
        "topic": record.get("topic"),
        "source_type": record.get("source_type"),
        "original_text": record.get("original_text"),
        "generated_markdown": record.get("generated_markdown"),
        "image_path": record.get("image_path"),
    }
    res = supabase.table("study_materials").insert(data).execute()
    return res.data[0]["id"]


def load_assignments(include_done: bool = True) -> pd.DataFrame:
    supabase = get_supabase()
    query = supabase.table("assignments").select("*")
    if not include_done:
        query = query.neq("status", "Done")
    res = query.order("due_date", desc=False).execute()
    df = pd.DataFrame(res.data or [])
    if not df.empty and "due_date" in df.columns:
        # Keep nulls at the end
        df = df.sort_values(by=["due_date", "due_time"], na_position="last")
    return df


def load_study_materials() -> pd.DataFrame:
    supabase = get_supabase()
    res = supabase.table("study_materials").select("*").order("created_at", desc=True).execute()
    return pd.DataFrame(res.data or [])


# -----------------------------
# Utility helpers
# -----------------------------
def safe_int(value: Any) -> Optional[int]:
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except Exception:
        return None


def today_iso() -> str:
    return date.today().isoformat()


def parse_iso_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def due_label(due_date: Optional[str], due_time: Optional[str] = None) -> str:
    d = parse_iso_date(due_date)
    if not d:
        return "No due date"
    today = date.today()
    time_part = f" at {due_time}" if due_time else ""
    if d == today:
        return f"Today{time_part}"
    if d == today + timedelta(days=1):
        return f"Tomorrow{time_part}"
    if d < today:
        return f"Overdue: {d.strftime('%b %-d')}{time_part}" if os.name != "nt" else f"Overdue: {d.strftime('%b %#d')}{time_part}"
    return f"{d.strftime('%a, %b %-d')}{time_part}" if os.name != "nt" else f"{d.strftime('%a, %b %#d')}{time_part}"


def priority_emoji(priority: Optional[str]) -> str:
    priority = (priority or "Normal").lower()
    if priority == "high":
        return "🔴"
    if priority == "low":
        return "🟢"
    return "🟡"


def status_emoji(status: Optional[str]) -> str:
    status = status or "Not started"
    if status == "Done":
        return "✅"
    if status == "In progress":
        return "🟦"
    return "⬜"


def image_to_data_url(uploaded_file: Any) -> Tuple[str, bytes, str]:
    data = uploaded_file.getvalue()
    mime_type = getattr(uploaded_file, "type", None) or "image/jpeg"
    b64 = base64.b64encode(data).decode("utf-8")
    return f"data:{mime_type};base64,{b64}", data, mime_type


def display_uploaded_image(uploaded_file: Any, caption: str = "Uploaded image") -> None:
    try:
        img = Image.open(io.BytesIO(uploaded_file.getvalue()))
        st.image(img, caption=caption, use_container_width=True)
    except Exception:
        st.info("Image uploaded. Preview is not available for this file type.")


def strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|markdown)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_json_from_text(text: str) -> Dict[str, Any]:
    text = strip_code_fence(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    try:
        value = st.secrets.get(name)
        if value:
            return str(value)
    except Exception:
        pass
    return os.getenv(name, default)


def openai_client() -> Optional[Any]:
    api_key = get_secret("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return None
    return OpenAI(api_key=api_key)


def ai_is_ready() -> bool:
    return openai_client() is not None


def call_openai_text(prompt: str, model: str = DEFAULT_MODEL) -> str:
    client = openai_client()
    if client is None:
        raise RuntimeError("OpenAI API key is not configured.")
    response = client.responses.create(
        model=model,
        input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
    )
    return response.output_text


def call_openai_image(prompt: str, uploaded_file: Any, model: str = DEFAULT_MODEL) -> str:
    client = openai_client()
    if client is None:
        raise RuntimeError("OpenAI API key is not configured.")
    data_url, _, _ = image_to_data_url(uploaded_file)
    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": data_url},
                ],
            }
        ],
    )
    return response.output_text


def assignment_extraction_prompt() -> str:
    return f"""
You are helping a high school student manage assignments.

Read the image. It may contain a classroom board, assignment sheet, syllabus, study guide, homework screenshot, or class notes.

Return ONLY valid JSON with this exact structure:
{{
  "class_name": string or null,
  "title": string or null,
  "description": string or null,
  "due_date": string or null,
  "due_time": string or null,
  "assignment_type": "Homework" | "Quiz" | "Test" | "Project" | "Reading" | "Essay" | "Other" | null,
  "estimated_effort_minutes": integer or null,
  "priority": "Low" | "Normal" | "High",
  "materials_needed": string or null,
  "uncertainty_notes": string or null
}}

Rules:
- Today is {date.today().isoformat()}.
- due_date must be ISO format YYYY-MM-DD.
- If a date is unclear, use null and explain in uncertainty_notes.
- Do not invent missing class names, dates, or instructions.
- Keep the title short and clear.
- Use High priority for tests, quizzes, major projects, essays, or anything due within 24 hours.
- estimated_effort_minutes should be a practical student estimate when supported or obvious; otherwise null.
""".strip()


def study_prompt_from_image(output_type: str) -> str:
    return f"""
You are helping an 11th grade student study from a photo of class notes, a study guide, worksheet, textbook page, or classroom handout.

Create a clear, student-friendly {output_type}.

Use this structure in markdown:

# Topic
A short topic title.

# Clean Summary
A concise explanation of the material in plain language.

# Key Terms
- Term: simple definition

# Study Guide
Organize the content by concept, with bullets and examples.

# Flashcards
Create 8 to 15 flashcards in this format:
Q: question
A: answer

# Practice Quiz
Create 5 to 10 questions. Include the answer key at the end.

# What to Study First
Give a short prioritized plan.

Rules:
- Use only information visible in the image.
- If the image is incomplete or unclear, say what needs to be checked.
- Keep it supportive and not overwhelming.
- For literature: go beyond plot — include themes, character motivation, conflict, evidence, and possible essay questions.
- For science/history: emphasize concepts, causes/effects, vocabulary in context, and application.
""".strip()


def study_prompt_from_text(text: str, output_type: str) -> str:
    return f"""
You are helping an 11th grade student study from the notes below.

Create a clear, student-friendly {output_type}.

NOTES:
{text}

Use this structure in markdown:

# Topic
A short topic title.

# Clean Summary
A concise explanation of the material in plain language.

# Key Terms
- Term: simple definition

# Study Guide
Organize the content by concept, with bullets and examples.

# Flashcards
Create 8 to 15 flashcards in this format:
Q: question
A: answer

# Practice Quiz
Create 5 to 10 questions. Include the answer key at the end.

# What to Study First
Give a short prioritized plan.

Rules:
- Use only information provided in the notes.
- If the notes are incomplete or unclear, say what needs to be checked.
- Keep it supportive and not overwhelming.
- For literature: go beyond plot — include themes, character motivation, conflict, evidence, and possible essay questions.
- For science/history: emphasize concepts, causes/effects, vocabulary in context, and application.
""".strip()


# -----------------------------
# Export helpers
# -----------------------------
def assignments_to_ics(df: pd.DataFrame) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Homework Hub//EN"]
    for _, row in df.iterrows():
        d = parse_iso_date(row.get("due_date"))
        if not d:
            continue
        uid = row.get("id") or str(uuid.uuid4())
        title = row.get("title") or "Assignment"
        class_name = row.get("class_name") or "School"
        description = row.get("description") or ""
        due_time = row.get("due_time")

        if due_time and re.match(r"^\d{1,2}:\d{2}", str(due_time)):
            hour, minute = [int(x) for x in str(due_time)[:5].split(":")]
            start_dt = datetime.combine(d, datetime.min.time()).replace(hour=hour, minute=minute)
            end_dt = start_dt + timedelta(minutes=30)
            dtstart = start_dt.strftime("%Y%m%dT%H%M%S")
            dtend = end_dt.strftime("%Y%m%dT%H%M%S")
        else:
            dtstart = d.strftime("%Y%m%d")
            dtend = (d + timedelta(days=1)).strftime("%Y%m%d")

        summary = escape_ics(f"{class_name}: {title}")
        desc = escape_ics(description)
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}@homework-hub",
                f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
                f"SUMMARY:{summary}",
                f"DESCRIPTION:{desc}",
                f"DTSTART:{dtstart}",
                f"DTEND:{dtend}",
                "END:VEVENT",
            ]
        )
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


def escape_ics(text: Any) -> str:
    text = str(text or "")
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


# -----------------------------
# UI components
# -----------------------------
def assignment_card(row: pd.Series, show_actions: bool = True) -> None:
    due = due_label(row.get("due_date"), row.get("due_time"))
    d = parse_iso_date(row.get("due_date"))
    due_class = "small-muted"
    if d and d < date.today() and row.get("status") != "Done":
        due_class = "due-today"
    elif d and d <= date.today() + timedelta(days=1) and row.get("status") != "Done":
        due_class = "due-soon"
    elif row.get("status") == "Done":
        due_class = "done"

    st.markdown(
        f"""
        <div class="assignment-card">
          <div><strong>{status_emoji(row.get('status'))} {priority_emoji(row.get('priority'))} {row.get('title') or 'Untitled assignment'}</strong></div>
          <div class="small-muted">{row.get('class_name') or 'No class'} • {row.get('assignment_type') or 'Assignment'}</div>
          <div class="{due_class}">Due: {due}</div>
          <div class="small-muted">{row.get('description') or ''}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if row.get("image_path"):
        with st.expander("View original photo"):
            display_stored_image(row.get("image_path"))

    if show_actions:
        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("Not started", key=f"ns_{row['id']}"):
                update_assignment_status(row["id"], "Not started")
                st.rerun()
        with col2:
            if st.button("In progress", key=f"ip_{row['id']}"):
                update_assignment_status(row["id"], "In progress")
                st.rerun()
        with col3:
            if st.button("Done", key=f"done_{row['id']}"):
                update_assignment_status(row["id"], "Done")
                st.rerun()


def render_header() -> None:
    st.markdown(f"<div class='big-title'>📚 {APP_NAME}</div>", unsafe_allow_html=True)
    st.markdown("<div class='subtitle'>One place for assignments, due dates, and study help.</div>", unsafe_allow_html=True)


def render_ai_notice() -> None:
    if ai_is_ready():
        st.success("AI features are on.", icon="✅")
    else:
        st.info(
            "AI features are off until OPENAI_API_KEY is added in Streamlit secrets. Manual assignment tracking still works.",
            icon="ℹ️",
        )


# -----------------------------
# Pages
# -----------------------------
def page_today() -> None:
    st.subheader("What’s due")
    df = load_assignments(include_done=False)

    if df.empty:
        st.markdown(
            """
            <div class="metric-card">
              <strong>No open assignments yet.</strong><br>
              <span class="small-muted">Use Add Assignment to enter one manually or from a photo.</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    today = date.today()
    df["due_date_parsed"] = df["due_date"].apply(parse_iso_date)

    overdue = df[df["due_date_parsed"].notna() & (df["due_date_parsed"] < today)]
    today_df = df[df["due_date_parsed"] == today]
    week_df = df[df["due_date_parsed"].notna() & (df["due_date_parsed"] > today) & (df["due_date_parsed"] <= today + timedelta(days=7))]
    no_date = df[df["due_date_parsed"].isna()]

    col1, col2, col3 = st.columns(3)
    col1.metric("Overdue", len(overdue))
    col2.metric("Today", len(today_df))
    col3.metric("Next 7 days", len(week_df))

    sections = [
        ("Overdue", overdue),
        ("Today", today_df),
        ("Next 7 days", week_df),
        ("No due date", no_date),
    ]
    for label, part in sections:
        if not part.empty:
            st.markdown(f"### {label}")
            for _, row in part.iterrows():
                assignment_card(row)


def page_add_assignment() -> None:
    st.subheader("Add Assignment")
    render_ai_notice()

    method = st.radio(
        "How do you want to add it?",
        ["Photo", "Manual"],
        horizontal=True,
    )

    extracted: Dict[str, Any] = {}
    source = "Manual entry"
    image_path = None
    uploaded = None

    if method == "Photo":
        st.write("Take a photo or upload a screenshot/photo of the assignment.")
        capture_mode = st.radio("Photo source", ["Camera", "Upload"], horizontal=True)
        if capture_mode == "Camera":
            uploaded = st.camera_input("Take a picture")
        else:
            uploaded = st.file_uploader("Upload image", type=["png", "jpg", "jpeg", "webp"])

        if uploaded is not None:
            display_uploaded_image(uploaded, "Assignment source")
            source = f"Photo: {getattr(uploaded, 'name', 'camera image')}"
            if ai_is_ready():
                if st.button("Extract assignment from photo", type="primary"):
                    with st.spinner("Reading the image..."):
                        try:
                            raw = call_openai_image(assignment_extraction_prompt(), uploaded)
                            extracted = parse_json_from_text(raw)
                            st.session_state["last_assignment_extract"] = extracted
                            st.session_state["last_uploaded_file"] = uploaded
                            st.success("Review the extracted details below before saving.")
                        except Exception as exc:
                            st.error(f"I could not extract the assignment: {exc}")
            else:
                st.warning("Add OPENAI_API_KEY in secrets to extract assignments from photos.")

    extracted = st.session_state.get("last_assignment_extract", extracted)
    uploaded = st.session_state.get("last_uploaded_file", uploaded)

    with st.form("assignment_form", clear_on_submit=False):
        st.markdown("### Review & save")
        class_name = st.text_input("Class", value=extracted.get("class_name") or "")
        title = st.text_input("Assignment title", value=extracted.get("title") or "")
        description = st.text_area("Description", value=extracted.get("description") or "", height=90)

        col1, col2 = st.columns(2)
        with col1:
            default_due = parse_iso_date(extracted.get("due_date"))
            due_date_value = st.date_input("Due date", value=default_due, format="YYYY-MM-DD") if default_due else st.date_input("Due date", value=None, format="YYYY-MM-DD")
        with col2:
            due_time = st.text_input("Due time", value=extracted.get("due_time") or "", placeholder="Example: 11:59 PM or 14:30")

        col3, col4 = st.columns(2)
        with col3:
            assignment_type = st.selectbox(
                "Type",
                ["Homework", "Quiz", "Test", "Project", "Reading", "Essay", "Other"],
                index=type_index(extracted.get("assignment_type")),
            )
        with col4:
            priority = st.selectbox("Priority", ["Low", "Normal", "High"], index=priority_index(extracted.get("priority")))

        col5, col6 = st.columns(2)
        with col5:
            effort = st.number_input(
                "Estimated minutes",
                min_value=0,
                max_value=600,
                step=5,
                value=safe_int(extracted.get("estimated_effort_minutes")) or 30,
            )
        with col6:
            status = st.selectbox("Status", ["Not started", "In progress", "Done"], index=0)

        uncertainty = st.text_area("Notes / uncertainty", value=extracted.get("uncertainty_notes") or "", height=70)

        submitted = st.form_submit_button("Save assignment", type="primary")
        if submitted:
            if not title.strip():
                st.error("Please add an assignment title before saving.")
            else:
                # Upload image if we have one
                if uploaded is not None:
                    with st.spinner("Saving photo..."):
                        image_path = upload_image_to_storage(uploaded, folder="assignments")

                add_assignment(
                    {
                        "class_name": class_name.strip(),
                        "title": title.strip(),
                        "description": description.strip(),
                        "due_date": due_date_value.isoformat() if isinstance(due_date_value, date) else None,
                        "due_time": due_time.strip(),
                        "assignment_type": assignment_type,
                        "estimated_effort_minutes": int(effort),
                        "priority": priority,
                        "status": status,
                        "source": source,
                        "uncertainty_notes": uncertainty.strip(),
                        "image_path": image_path,
                    }
                )
                st.session_state.pop("last_assignment_extract", None)
                st.session_state.pop("last_uploaded_file", None)
                st.success("Assignment saved (with photo if provided).")
                st.rerun()


def type_index(value: Optional[str]) -> int:
    options = ["Homework", "Quiz", "Test", "Project", "Reading", "Essay", "Other"]
    try:
        return options.index(value or "Homework")
    except ValueError:
        return 0


def priority_index(value: Optional[str]) -> int:
    options = ["Low", "Normal", "High"]
    try:
        return options.index(value or "Normal")
    except ValueError:
        return 1


def page_study_tools() -> None:
    st.subheader("Study Tools")
    render_ai_notice()

    source_type = st.radio("Add material by", ["Photo", "Text"], horizontal=True)
    class_name = st.text_input("Class", placeholder="Example: Biology")
    topic = st.text_input("Topic", placeholder="Example: Cell division")
    output_type = st.selectbox("Create", ["complete study guide", "flashcards and quiz", "clean summary"])

    uploaded = None
    notes_text = ""

    if source_type == "Photo":
        capture_mode = st.radio("Photo source", ["Camera", "Upload"], horizontal=True, key="study_photo_source")
        if capture_mode == "Camera":
            uploaded = st.camera_input("Take a picture of notes or study guide", key="study_camera")
        else:
            uploaded = st.file_uploader("Upload notes image", type=["png", "jpg", "jpeg", "webp"], key="study_upload")
        if uploaded is not None:
            display_uploaded_image(uploaded, "Study material")
    else:
        notes_text = st.text_area("Paste notes here", height=220)

    if st.button("Generate study help", type="primary"):
        if not ai_is_ready():
            st.error("Add OPENAI_API_KEY in Streamlit secrets to generate study tools.")
            return
        if source_type == "Photo" and uploaded is None:
            st.error("Please add a photo first.")
            return
        if source_type == "Text" and not notes_text.strip():
            st.error("Please paste notes first.")
            return

        with st.spinner("Creating study materials..."):
            try:
                image_path = None
                if source_type == "Photo":
                    generated = call_openai_image(study_prompt_from_image(output_type), uploaded)
                    original_text = f"Photo: {getattr(uploaded, 'name', 'camera image')}"
                    image_path = upload_image_to_storage(uploaded, folder="study")
                else:
                    generated = call_openai_text(study_prompt_from_text(notes_text, output_type))
                    original_text = notes_text

                add_study_material(
                    {
                        "class_name": class_name.strip(),
                        "topic": topic.strip(),
                        "source_type": source_type,
                        "original_text": original_text,
                        "generated_markdown": generated,
                        "image_path": image_path,
                    }
                )
                st.session_state["last_study_output"] = generated
                st.success("Study material created and saved (with photo if provided).")
            except Exception as exc:
                st.error(f"I could not generate study materials: {exc}")

    if "last_study_output" in st.session_state:
        st.markdown("### Latest study output")
        st.markdown(st.session_state["last_study_output"])

    st.markdown("### Saved study materials")
    materials = load_study_materials()
    if materials.empty:
        st.info("No saved study materials yet.")
    else:
        for _, row in materials.head(10).iterrows():
            with st.expander(f"{row.get('class_name') or 'Class'} — {row.get('topic') or 'Study material'}"):
                if row.get("image_path"):
                    display_stored_image(row.get("image_path"), caption="Original photo")
                st.markdown(row.get("generated_markdown") or "")
                st.download_button(
                    "Download as Markdown",
                    data=(row.get("generated_markdown") or "").encode("utf-8"),
                    file_name=f"study_material_{row.get('id')}.md",
                    mime="text/markdown",
                    key=f"download_md_{row.get('id')}",
                )


def page_calendar() -> None:
    st.subheader("Calendar")
    df = load_assignments(include_done=False)
    if df.empty:
        st.info("No open assignments to show on the calendar.")
        return

    today = date.today()
    col1, col2 = st.columns(2)
    with col1:
        year = st.number_input("Year", min_value=today.year - 1, max_value=today.year + 2, value=today.year, step=1)
    with col2:
        month = st.selectbox("Month", list(range(1, 13)), index=today.month - 1, format_func=lambda m: calendar.month_name[m])

    assignments_by_date: Dict[str, List[pd.Series]] = {}
    for _, row in df.iterrows():
        d = parse_iso_date(row.get("due_date"))
        if d and d.year == int(year) and d.month == int(month):
            assignments_by_date.setdefault(d.isoformat(), []).append(row)

    cal = calendar.Calendar(firstweekday=6)
    st.markdown(f"### {calendar.month_name[int(month)]} {int(year)}")
    for week in cal.monthdatescalendar(int(year), int(month)):
        cols = st.columns(7)
        for idx, day in enumerate(week):
            with cols[idx]:
                if day.month != int(month):
                    st.markdown(f"<span class='small-muted'>{day.day}</span>", unsafe_allow_html=True)
                    continue
                is_today = day == today
                label = f"**{day.day}**" if is_today else str(day.day)
                st.markdown(label)
                for row in assignments_by_date.get(day.isoformat(), [])[:3]:
                    st.caption(f"{priority_emoji(row.get('priority'))} {row.get('class_name') or ''}: {row.get('title') or ''}"[:60])
                if len(assignments_by_date.get(day.isoformat(), [])) > 3:
                    st.caption("More...")

    st.markdown("### Export")
    ics = assignments_to_ics(df)
    st.download_button("Download calendar file (.ics)", data=ics.encode("utf-8"), file_name="homework_hub_assignments.ics", mime="text/calendar")


def page_all_assignments() -> None:
    st.subheader("All Assignments")
    include_done = st.toggle("Show completed", value=True)
    df = load_assignments(include_done=include_done)
    if df.empty:
        st.info("No assignments found.")
        return

    for _, row in df.iterrows():
        assignment_card(row, show_actions=True)
        with st.expander("More options", expanded=False):
            st.write(f"Created: {row.get('created_at')}")
            st.write(f"Source: {row.get('source') or 'Not captured'}")
            if row.get("uncertainty_notes"):
                st.warning(row.get("uncertainty_notes"))
            if st.button("Delete assignment", key=f"delete_{row['id']}"):
                delete_assignment(row["id"])
                st.success("Assignment deleted.")
                st.rerun()

    st.markdown("### Download")
    st.download_button("Download assignments CSV", data=df_to_csv_bytes(df), file_name="homework_hub_assignments.csv", mime="text/csv")


def page_settings() -> None:
    st.subheader("Settings")
    render_ai_notice()
    st.markdown(
        """
        ### Add to iPhone Home Screen
        1. Open this Streamlit app in Safari on the iPhone.
        2. Tap the Share button.
        3. Choose **Add to Home Screen**.
        4. Name it **Homework Hub**.

        ### Data
        Assignments, study materials, and original photos are now stored in **Supabase** (persistent).
        """
    )

    df = load_assignments(include_done=True)
    materials = load_study_materials()
    st.download_button("Backup assignments CSV", data=df_to_csv_bytes(df), file_name="homework_hub_assignments_backup.csv", mime="text/csv")
    st.download_button("Backup study materials CSV", data=df_to_csv_bytes(materials), file_name="homework_hub_study_materials_backup.csv", mime="text/csv")


# -----------------------------
# Main app
# -----------------------------
def main() -> None:
    render_header()

    page = st.radio(
        "Navigation",
        ["Today", "Add", "Study", "Calendar", "All", "Settings"],
        horizontal=True,
        label_visibility="collapsed",
    )

    if page == "Today":
        page_today()
    elif page == "Add":
        page_add_assignment()
    elif page == "Study":
        page_study_tools()
    elif page == "Calendar":
        page_calendar()
    elif page == "All":
        page_all_assignments()
    elif page == "Settings":
        page_settings()


if __name__ == "__main__":
    main()
