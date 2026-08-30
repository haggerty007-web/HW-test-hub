import base64
import calendar
import hashlib
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
from PIL import Image, ImageOps
from supabase import create_client, Client

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

APP_NAME = "Locked In"
APP_VERSION = "Locked In v7.3.4-camera-restore"
DEFAULT_MODEL = "gpt-5.6-sol"
PLANNER_MODEL = "gpt-5.6-terra"
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
def get_supabase() -> Client:
    """
    Create a Supabase client for the current Streamlit session.

    v7 intentionally does NOT cache this globally because each student must
    have a separate authenticated Supabase session.
    """
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    client = create_client(url, key)

    access_token = st.session_state.get("sb_access_token")
    refresh_token = st.session_state.get("sb_refresh_token")

    if access_token and refresh_token:
        try:
            client.auth.set_session(access_token, refresh_token)
        except Exception:
            clear_auth_state()

    return client


def clear_auth_state() -> None:
    for key in [
        "sb_access_token",
        "sb_refresh_token",
        "user_id",
        "user_email",
        "locked_in_assignment_id",
    ]:
        st.session_state.pop(key, None)


def current_user_id() -> Optional[str]:
    return st.session_state.get("user_id")


def require_user_id() -> str:
    user_id = current_user_id()
    if not user_id:
        raise RuntimeError("Please sign in first.")
    return user_id


def save_auth_session(auth_response: Any) -> bool:
    session = getattr(auth_response, "session", None)
    user = getattr(auth_response, "user", None)

    if not session or not user:
        return False

    st.session_state["sb_access_token"] = session.access_token
    st.session_state["sb_refresh_token"] = session.refresh_token
    st.session_state["user_id"] = str(user.id)
    st.session_state["user_email"] = getattr(user, "email", "") or ""
    return True


def render_auth_gate() -> bool:
    """Return True only when a student is signed in."""
    if current_user_id():
        return True

    st.title(APP_NAME)
    st.caption("Your schoolwork. Your plan. One thing at a time.")
    st.markdown("### Welcome to Locked In")

    sign_in_tab, create_tab = st.tabs(["Sign in", "Create account"])

    with sign_in_tab:
        with st.form("signin_form"):
            email = st.text_input("Email", key="signin_email")
            password = st.text_input(
                "Password",
                type="password",
                key="signin_password",
            )
            submitted = st.form_submit_button("Sign in", type="primary")

        if submitted:
            try:
                client = create_client(
                    st.secrets["SUPABASE_URL"],
                    st.secrets["SUPABASE_KEY"],
                )
                response = client.auth.sign_in_with_password(
                    {"email": email.strip(), "password": password}
                )
                if save_auth_session(response):
                    st.rerun()
                else:
                    st.error("I could not start your session.")
            except Exception as exc:
                st.error(f"Sign in failed: {exc}")

    with create_tab:
        st.caption(
            "Beta accounts keep each student's assignments and study materials separate."
        )
        with st.form("signup_form"):
            email = st.text_input("Email", key="signup_email")
            password = st.text_input(
                "Create password",
                type="password",
                key="signup_password",
            )
            submitted = st.form_submit_button(
                "Create account",
                type="primary",
            )

        if submitted:
            try:
                client = create_client(
                    st.secrets["SUPABASE_URL"],
                    st.secrets["SUPABASE_KEY"],
                )
                response = client.auth.sign_up(
                    {"email": email.strip(), "password": password}
                )
                if save_auth_session(response):
                    st.success("Account created.")
                    st.rerun()
                else:
                    st.success(
                        "Account created. Check your email to confirm it, then sign in."
                    )
            except Exception as exc:
                st.error(f"Account creation failed: {exc}")

    return False


def upload_image_to_storage(uploaded_file: Any, folder: str = "assignments") -> Optional[str]:
    """Upload image to Supabase Storage and return the path."""
    try:
        supabase = get_supabase()
        file_bytes = uploaded_file.getvalue()
        ext = getattr(uploaded_file, "type", "image/jpeg").split("/")[-1]
        if ext not in ["jpeg", "jpg", "png", "webp"]:
            ext = "jpg"
        filename = f"{require_user_id()}/{folder}/{uuid.uuid4()}.{ext}"

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
        "user_id": require_user_id(),
    }
    res = supabase.table("assignments").insert(data).execute()
    return res.data[0]["id"]


def update_assignment_status(assignment_id: str, status: str) -> None:
    supabase = get_supabase()
    supabase.table("assignments").update({"status": status}).eq("id", assignment_id).eq("user_id", require_user_id()).execute()


def delete_assignment(assignment_id: str) -> None:
    supabase = get_supabase()
    # Optionally also delete the image from storage here
    supabase.table("assignments").delete().eq("id", assignment_id).eq("user_id", require_user_id()).execute()


def add_study_material(record: Dict[str, Any]) -> str:
    supabase = get_supabase()
    data = {
        "class_name": record.get("class_name"),
        "topic": record.get("topic"),
        "source_type": record.get("source_type"),
        "original_text": record.get("original_text"),
        "generated_markdown": record.get("generated_markdown"),
        "image_path": record.get("image_path"),
        "user_id": require_user_id(),
    }
    res = supabase.table("study_materials").insert(data).execute()
    return res.data[0]["id"]


def load_assignments(include_done: bool = True) -> pd.DataFrame:
    supabase = get_supabase()
    query = supabase.table("assignments").select("*").eq("user_id", require_user_id())
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
    res = supabase.table("study_materials").select("*").eq("user_id", require_user_id()).order("created_at", desc=True).execute()
    return pd.DataFrame(res.data or [])


def load_my_classes() -> List[str]:
    supabase = get_supabase()
    res = (
        supabase.table("student_classes")
        .select("class_name")
        .eq("user_id", require_user_id())
        .order("sort_order", desc=False)
        .execute()
    )
    return [
        str(row.get("class_name")).strip()
        for row in (res.data or [])
        if row.get("class_name")
    ]


def add_my_class(class_name: str) -> None:
    name = class_name.strip()
    if not name:
        return

    supabase = get_supabase()
    existing = load_my_classes()
    if name.lower() in {c.lower() for c in existing}:
        return

    supabase.table("student_classes").insert(
        {
            "user_id": require_user_id(),
            "class_name": name,
            "sort_order": len(existing),
        }
    ).execute()


def delete_my_class(class_name: str) -> None:
    supabase = get_supabase()
    (
        supabase.table("student_classes")
        .delete()
        .eq("user_id", require_user_id())
        .eq("class_name", class_name)
        .execute()
    )



def transcribe_voice_note(audio_file: Any) -> str:
    client = openai_client()
    if client is None:
        raise RuntimeError("OpenAI API key is not configured.")

    # Streamlit's st.audio_input returns an UploadedFile-like object.
    audio_bytes = io.BytesIO(audio_file.getvalue())
    audio_bytes.name = getattr(audio_file, "name", "locked_in_voice.wav") or "locked_in_voice.wav"

    result = client.audio.transcriptions.create(
        model="gpt-4o-mini-transcribe",
        file=audio_bytes,
    )

    transcript = getattr(result, "text", None)
    if not transcript:
        raise RuntimeError("I could not transcribe that recording.")

    return str(transcript).strip()


def interpret_school_updates(
    transcript: str,
    existing_assignments: pd.DataFrame,
    classes: List[str],
) -> Dict[str, Any]:
    client = openai_client()
    if client is None:
        raise RuntimeError("OpenAI API key is not configured.")

    existing = []
    if not existing_assignments.empty:
        for _, row in existing_assignments.iterrows():
            existing.append(
                {
                    "id": str(row.get("id")),
                    "class_name": row.get("class_name"),
                    "title": row.get("title"),
                    "due_date": row.get("due_date"),
                    "status": row.get("status"),
                }
            )

    schema = {
        "type": "object",
        "properties": {
            "operations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["add", "reschedule", "complete"],
                        },
                        "assignment_id": {"type": "string"},
                        "class_name": {"type": "string"},
                        "title": {"type": "string"},
                        "due_date": {"type": "string"},
                        "assignment_type": {
                            "type": "string",
                            "enum": [
                                "Homework",
                                "Quiz",
                                "Test",
                                "Project",
                                "Reading",
                                "Essay",
                                "Other",
                            ],
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                        "note": {"type": "string"},
                    },
                    "required": [
                        "action",
                        "assignment_id",
                        "class_name",
                        "title",
                        "due_date",
                        "assignment_type",
                        "confidence",
                        "note",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["operations"],
        "additionalProperties": False,
    }

    prompt = f"""
You convert a student's spoken school update into proposed changes for the
Locked In assignment list.

TODAY: {date.today().isoformat()}

STUDENT'S CLASSES:
{json.dumps(classes, ensure_ascii=False)}

EXISTING OPEN ASSIGNMENTS:
{json.dumps(existing, ensure_ascii=False)}

STUDENT SAID:
{transcript}

Return proposed operations only. Nothing is applied automatically.

Rules:
- action "add": create a new assignment.
- action "reschedule": change the due date of an existing assignment.
- action "complete": mark an existing assignment Done.
- For reschedule/complete, assignment_id MUST exactly match an ID from the
  existing assignment list.
- For add, assignment_id must be an empty string.
- Resolve relative dates such as "Friday", "tomorrow", or "next Tuesday"
  using TODAY.
- due_date must be YYYY-MM-DD when a date is known, otherwise empty string.
- For complete, due_date may be empty.
- Use the student's existing class names when possible.
- Do not invent an assignment the student did not mention.
- If an existing assignment match is ambiguous, use confidence "low" and
  explain the ambiguity in note.
- Keep titles short and close to the student's wording.
- Infer Quiz/Test/Essay/etc. only when the student's wording supports it;
  otherwise use Other.
""".strip()

    response = client.responses.create(
        model=DEFAULT_MODEL,
        input=[
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "locked_in_school_updates",
                "strict": True,
                "schema": schema,
            }
        },
    )

    return json.loads(response.output_text)


def update_assignment_due_date(
    assignment_id: str,
    due_date: str,
) -> None:
    supabase = get_supabase()
    (
        supabase.table("assignments")
        .update({"due_date": due_date})
        .eq("id", assignment_id)
        .eq("user_id", require_user_id())
        .execute()
    )


def apply_voice_operations(operations: List[Dict[str, Any]]) -> int:
    applied = 0

    for op in operations:
        action = op.get("action")

        if action == "add":
            add_assignment(
                {
                    "class_name": op.get("class_name") or None,
                    "title": op.get("title") or "Assignment",
                    "description": "",
                    "due_date": op.get("due_date") or None,
                    "due_time": "",
                    "assignment_type": op.get("assignment_type") or "Other",
                    "estimated_effort_minutes": 30,
                    "priority": (
                        "High"
                        if op.get("assignment_type") in {"Quiz", "Test", "Project", "Essay"}
                        else "Normal"
                    ),
                    "status": "Not started",
                    "source": "Voice",
                    "uncertainty_notes": op.get("note") or "",
                    "image_path": None,
                }
            )
            applied += 1

        elif action == "reschedule":
            assignment_id = op.get("assignment_id") or ""
            due_date = op.get("due_date") or ""
            if assignment_id and due_date:
                update_assignment_due_date(assignment_id, due_date)
                applied += 1

        elif action == "complete":
            assignment_id = op.get("assignment_id") or ""
            if assignment_id:
                update_assignment_status(assignment_id, "Done")
                applied += 1

    return applied


def render_tell_locked_in() -> None:
    st.markdown("### 🎙️ Tell Locked In")
    st.caption(
        "Say what changed. Locked In will show you the proposed updates before saving anything."
    )

    classes = load_my_classes()
    open_assignments = load_assignments(include_done=False)

    if not classes:
        st.info(
            "Add your classes in Settings first so voice updates can match assignments reliably."
        )

    audio = st.audio_input("Record a school update")

    if audio is not None:
        st.audio(audio)

        if st.button("Understand my update", type="primary"):
            with st.spinner("Listening and organizing..."):
                try:
                    transcript = transcribe_voice_note(audio)
                    st.session_state["voice_transcript"] = transcript

                    result = interpret_school_updates(
                        transcript,
                        open_assignments,
                        classes,
                    )
                    st.session_state["voice_operations"] = result.get(
                        "operations",
                        [],
                    )
                except Exception as exc:
                    st.error(f"I couldn't understand that update: {exc}")

    transcript = st.session_state.get("voice_transcript")
    operations = st.session_state.get("voice_operations", [])

    if transcript:
        st.markdown("#### I heard")
        st.info(transcript)

    if operations:
        st.markdown("#### Proposed changes")
        st.caption("Nothing changes until you tap Confirm.")

        for i, op in enumerate(operations, start=1):
            action = op.get("action")
            confidence = op.get("confidence")

            if action == "add":
                st.write(
                    f"**{i}. ADD** — {op.get('class_name') or 'Class'}: "
                    f"{op.get('title') or 'Assignment'}"
                )
                st.caption(
                    f"Due {op.get('due_date') or 'date not specified'} • "
                    f"{op.get('assignment_type') or 'Other'}"
                )

            elif action == "reschedule":
                st.write(
                    f"**{i}. CHANGE DATE** — {op.get('class_name') or 'Class'}: "
                    f"{op.get('title') or 'Assignment'} → "
                    f"{op.get('due_date') or 'date unclear'}"
                )

            elif action == "complete":
                st.write(
                    f"**{i}. COMPLETE** — {op.get('class_name') or 'Class'}: "
                    f"{op.get('title') or 'Assignment'}"
                )

            if confidence != "high" or op.get("note"):
                st.caption(
                    f"Confidence: {confidence}. {op.get('note') or ''}"
                )

        col_confirm, col_cancel = st.columns(2)

        with col_confirm:
            if st.button("Confirm changes", type="primary"):
                try:
                    count = apply_voice_operations(operations)
                    st.session_state.pop("voice_transcript", None)
                    st.session_state.pop("voice_operations", None)
                    st.success(f"Applied {count} change(s).")
                    st.rerun()
                except Exception as exc:
                    st.error(f"I couldn't apply the changes: {exc}")

        with col_cancel:
            if st.button("Cancel"):
                st.session_state.pop("voice_transcript", None)
                st.session_state.pop("voice_operations", None)
                st.rerun()



def add_test_review(record: Dict[str, Any]) -> str:
    supabase = get_supabase()
    data = {
        "user_id": require_user_id(),
        "class_name": record.get("class_name"),
        "test_name": record.get("test_name"),
        "test_date": record.get("test_date"),
        "score_text": record.get("score_text"),
        "analysis_markdown": record.get("analysis_markdown"),
        "patterns_json": record.get("patterns_json") or {},
        "image_path": record.get("image_path"),
    }
    res = supabase.table("test_reviews").insert(data).execute()
    return res.data[0]["id"]


def load_test_reviews(class_name: Optional[str] = None) -> pd.DataFrame:
    supabase = get_supabase()
    query = (
        supabase.table("test_reviews")
        .select("*")
        .eq("user_id", require_user_id())
        .order("created_at", desc=True)
    )

    if class_name:
        query = query.eq("class_name", class_name)

    res = query.execute()
    return pd.DataFrame(res.data or [])


def test_review_prompt(
    class_name: str,
    test_name: str,
    score_text: str,
) -> str:
    return f"""
You are Locked In, an AI academic coach reviewing a returned test with a student.

CLASS: {class_name or "Unknown"}
TEST: {test_name or "Returned test"}
SCORE: {score_text or "Not provided"}

The uploaded image shows a returned test, teacher markings, and/or the
student's work.

Your goals:
1. Identify missed or partially correct problems that are clearly visible.
2. Explain each mistake in student-friendly language.
3. Classify visible mistakes when appropriate as:
   - concept misunderstanding
   - setup/translation error
   - algebra/arithmetic error
   - sign error
   - skipped step
   - careless/rushed error
   - unclear handwriting/teacher marking
4. Preserve uncertainty. Never invent a question or answer that is not readable.
5. Give a short "What to remember next time" section.
6. Give 3-5 targeted practice recommendations based only on visible evidence.
7. Never label the student as "bad" at a subject. Describe specific observed mistakes.

Return markdown with this structure:

# Test Review

## What Went Well
- ...

## Questions to Review
### Question / problem
- What happened:
- Why it happened:
- How to avoid it next time:

## Patterns I Noticed
- ...

## What to Remember Next Time
1. ...
2. ...
3. ...

## Practice Next
- ...

At the very end include a fenced JSON block:

```json
{{
  "patterns": [
    {{
      "category": "concept",
      "observation": "specific observed mistake",
      "confidence": "high"
    }}
  ]
}}
```

Allowed categories:
concept, setup, arithmetic, sign, skipped_step, careless, unclear

Allowed confidence:
high, medium, low

Use only what is visible in the uploaded test image.
""".strip()


def parse_patterns_from_review(markdown_text: str) -> Dict[str, Any]:
    matches = re.findall(
        r"```json\s*(\{.*?\})\s*```",
        markdown_text,
        flags=re.DOTALL,
    )

    if not matches:
        return {"patterns": []}

    try:
        return json.loads(matches[-1])
    except Exception:
        return {"patterns": []}


def build_learning_profile_context(class_name: str) -> str:
    if not class_name:
        return ""

    try:
        reviews = load_test_reviews(class_name=class_name)
    except Exception:
        return ""

    if reviews.empty:
        return ""

    observations = []

    for _, row in reviews.head(5).iterrows():
        patterns = row.get("patterns_json") or {}

        if isinstance(patterns, str):
            try:
                patterns = json.loads(patterns)
            except Exception:
                patterns = {}

        for item in patterns.get("patterns", []):
            obs = str(item.get("observation") or "").strip()
            if obs:
                observations.append(obs)

    if not observations:
        return ""

    bullets = "\n".join(
        f"- {obs}"
        for obs in observations[:10]
    )

    return f"""
PAST TEST-REVIEW OBSERVATIONS FOR THIS CLASS:
{bullets}

Use these as gentle reminders only.
Do not assume a mistake pattern is permanent.
If the student's current work does not show the same issue, do not force it.
""".strip()


def render_test_review_tool() -> None:
    st.markdown("### 📄 Review a Test")
    st.caption(
        "Take a picture of a returned test. Locked In will explain mistakes, "
        "identify useful patterns, and remember them for future studying."
    )

    classes = load_my_classes()
    class_options = classes if classes else ["Algebra"]

    class_name = st.selectbox(
        "Class",
        class_options,
        key="review_test_class",
    )

    test_name = st.text_input(
        "Test name",
        placeholder="Example: Algebra Unit 1 Test",
        key="review_test_name",
    )

    score_text = st.text_input(
        "Score (optional)",
        placeholder="Example: 84% or 42/50",
        key="review_test_score",
    )

    st.info(
        "For worksheets and returned tests, use **Choose full-quality photo**. "
        "On iPhone, select the picture from Photos (or use the iPhone camera from "
        "the file picker). This usually preserves much more detail than the "
        "browser camera capture."
    )

    test_images = st.file_uploader(
        "Choose full-quality photo(s)",
        type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True,
        key="review_test_upload_hq",
    )

    if test_images:
        st.caption(f"{len(test_images)} photo(s) selected.")

        for i, test_image in enumerate(test_images, start=1):
            display_uploaded_image(
                test_image,
                f"Returned test — page {i}",
            )

            width, height = uploaded_image_dimensions(test_image)
            if width and height:
                st.caption(
                    f"Page {i}: {width} × {height} pixels."
                )
                if max(width, height) < 1600:
                    st.warning(
                        f"Page {i} is fairly small. For handwritten math, choose "
                        "the original photo from the iPhone Photos library if possible."
                    )

        if st.button(
            "Review this test",
            type="primary",
            key="review_test_submit",
        ):
            if not ai_is_ready():
                st.error("AI is not configured.")
                return

            with st.spinner("Reviewing the test..."):
                try:
                    analysis = call_openai_images_high_detail(
                        test_review_prompt(
                            class_name=class_name,
                            test_name=test_name,
                            score_text=score_text,
                        ),
                        test_images,
                    )

                    patterns = parse_patterns_from_review(
                        analysis
                    )

                    saved_paths = []
                    for test_image in test_images:
                        path = upload_image_to_storage(
                            test_image,
                            folder="test-reviews",
                        )
                        if path:
                            saved_paths.append(path)

                    image_path = saved_paths[0] if saved_paths else None

                    add_test_review(
                        {
                            "class_name": class_name,
                            "test_name": (
                                test_name.strip()
                                or "Returned test"
                            ),
                            "test_date": date.today().isoformat(),
                            "score_text": score_text.strip(),
                            "analysis_markdown": analysis,
                            "patterns_json": patterns,
                            "image_path": image_path,
                        }
                    )

                    st.session_state[
                        "latest_test_review"
                    ] = analysis

                    st.success(
                        "Test review saved. Locked In can now use "
                        "these observations when helping with future work."
                    )

                except Exception as exc:
                    st.error(
                        f"I couldn't review the test: {exc}"
                    )

    latest = st.session_state.get(
        "latest_test_review"
    )

    if latest:
        st.markdown("#### Latest review")
        st.markdown(latest)

    reviews = load_test_reviews(
        class_name=class_name
    )

    if not reviews.empty:
        st.markdown("#### Previous reviews")

        for _, row in reviews.head(5).iterrows():
            label = (
                row.get("test_name")
                or "Returned test"
            )

            with st.expander(str(label)):
                if row.get("score_text"):
                    st.caption(
                        f"Score: {row.get('score_text')}"
                    )

                if row.get("image_path"):
                    display_stored_image(
                        row.get("image_path"),
                        caption="Returned test",
                    )

                st.markdown(
                    row.get("analysis_markdown")
                    or ""
                )


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


def call_openai_image_high_detail(
    prompt: str,
    uploaded_file: Any,
    model: str = DEFAULT_MODEL,
) -> str:
    """
    Use high-detail vision for worksheets/tests where small handwriting,
    equations, teacher markings, and problem numbers matter.
    """
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
                    {
                        "type": "input_image",
                        "image_url": data_url,
                        "detail": "high",
                    },
                ],
            }
        ],
    )
    return response.output_text


def call_openai_images_high_detail(
    prompt: str,
    uploaded_files: List[Any],
    model: str = DEFAULT_MODEL,
) -> str:
    """
    Send multiple homework/test pages to the vision model at high detail.
    """
    client = openai_client()
    if client is None:
        raise RuntimeError("OpenAI API key is not configured.")

    content = [{"type": "input_text", "text": prompt}]

    for uploaded_file in uploaded_files:
        data_url, _, _ = image_to_data_url(uploaded_file)
        content.append(
            {
                "type": "input_image",
                "image_url": data_url,
                "detail": "high",
            }
        )

    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "user",
                "content": content,
            }
        ],
    )
    return response.output_text


def uploaded_image_dimensions(uploaded_file: Any) -> Tuple[int, int]:
    try:
        img = Image.open(io.BytesIO(uploaded_file.getvalue()))
        img = ImageOps.exif_transpose(img)
        return img.size
    except Exception:
        return (0, 0)



def normalized_planner_image(uploaded_file: Any) -> Image.Image:
    """
    Normalize the weekly planner photo into landscape orientation.

    This planner uses the same printed template each week. Class order may
    change, but the seven row bands and five weekday columns stay fixed.
    """
    raw = uploaded_file.getvalue()
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)

    if img.height > img.width:
        img = img.rotate(90, expand=True)

    if img.mode != "RGB":
        img = img.convert("RGB")

    return img


def pil_image_to_jpeg_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=94)
    return buf.getvalue()


def call_openai_images(
    prompt: str,
    image_bytes_list: List[bytes],
    model: str = PLANNER_MODEL,
) -> str:
    client = openai_client()
    if client is None:
        raise RuntimeError("OpenAI API key is not configured.")

    content = [{"type": "input_text", "text": prompt}]

    for image_bytes in image_bytes_list:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{b64}",
            }
        )

    response = client.responses.create(
        model=model,
        reasoning={"effort": "low"},
        max_output_tokens=600,
        input=[{"role": "user", "content": content}],
    )
    return response.output_text


def call_openai_images_structured(
    prompt: str,
    image_bytes_list: List[bytes],
    schema_name: str,
    schema: Dict[str, Any],
    model: str = PLANNER_MODEL,
) -> Dict[str, Any]:
    """
    Use OpenAI Structured Outputs so planner JSON cannot be malformed.
    """
    client = openai_client()
    if client is None:
        raise RuntimeError("OpenAI API key is not configured.")

    content = [{"type": "input_text", "text": prompt}]

    for image_bytes in image_bytes_list:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{b64}",
            }
        )

    response = client.responses.create(
        model=model,
        reasoning={"effort": "low"},
        max_output_tokens=600,
        input=[{"role": "user", "content": content}],
        text={
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": schema,
            }
        },
    )

    if not response.output_text:
        raise RuntimeError("The planner reader returned no structured output.")

    return json.loads(response.output_text)


# Fixed template geometry, expressed as fractions of the normalized landscape
# photo. These values are intentionally conservative and crop INSIDE the
# planner cells so neighboring rows/columns are excluded.
PLANNER_SUBJECTS = [
    "English",
    "Science",
    "Art",
    "Algebra",
    "Theo",
    "Spanish",
    "History",
]

# Calibrated against Meghan's actual planner photo.
# Coordinates are fractions of the normalized landscape image.
PLANNER_DAY_X = {
    "Monday": (0.095, 0.254),
    "Tuesday": (0.257, 0.402),
    "Wednesday": (0.525, 0.664),
    "Thursday": (0.667, 0.811),
    "Friday": (0.814, 0.967),
}

# Seven subject rows, top to bottom.
PLANNER_ROW_BOUNDS_Y = [
    0.132,
    0.213,
    0.295,
    0.378,
    0.461,
    0.544,
    0.627,
    0.710,
]


def crop_fraction(
    img: Image.Image,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> Image.Image:
    w, h = img.size
    return img.crop(
        (
            max(0, int(w * x1)),
            max(0, int(h * y1)),
            min(w, int(w * x2)),
            min(h, int(h * y2)),
        )
    )


def planner_structure_prompt() -> str:
    allowed = json.dumps(PLANNER_SUBJECTS, ensure_ascii=False)

    return f"""
Read ONLY the printed weekday dates and the seven class labels from this
weekly planner. Do not extract assignments.

Allowed subjects are exactly:
{allowed}

Return ONLY valid JSON:
{{
  "dates": {{
    "Monday": "YYYY-MM-DD",
    "Tuesday": "YYYY-MM-DD",
    "Wednesday": "YYYY-MM-DD",
    "Thursday": "YYYY-MM-DD",
    "Friday": "YYYY-MM-DD"
  }},
  "classes": [
    "subject for row 1",
    "subject for row 2",
    "subject for row 3",
    "subject for row 4",
    "subject for row 5",
    "subject for row 6",
    "subject for row 7"
  ]
}}

Rules:
- Today is {date.today().isoformat()}.
- Read the PRINTED weekday/date headers.
- Convert each date to YYYY-MM-DD.
- The seven class rows may be in a different order each week.
- Every class MUST be chosen from the allowed subject list.
- Use each allowed subject exactly once.
- Never return Math, Music, Band, or any subject outside the allowed list.
- Do not read assignment handwriting in this pass.
""".strip()


def read_planner_structure(uploaded_file: Any) -> Dict[str, Any]:
    """
    Read only the printed weekday dates and dynamic subject-row order.

    Uses Structured Outputs so malformed JSON cannot break the scan.
    """
    img = normalized_planner_image(uploaded_file)

    schema = {
        "type": "object",
        "properties": {
            "dates": {
                "type": "object",
                "properties": {
                    "Monday": {"type": "string"},
                    "Tuesday": {"type": "string"},
                    "Wednesday": {"type": "string"},
                    "Thursday": {"type": "string"},
                    "Friday": {"type": "string"},
                },
                "required": [
                    "Monday",
                    "Tuesday",
                    "Wednesday",
                    "Thursday",
                    "Friday",
                ],
                "additionalProperties": False,
            },
            "classes": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": PLANNER_SUBJECTS,
                },
                "minItems": 7,
                "maxItems": 7,
            },
        },
        "required": ["dates", "classes"],
        "additionalProperties": False,
    }

    structure = call_openai_images_structured(
        planner_structure_prompt(),
        [pil_image_to_jpeg_bytes(img)],
        schema_name="planner_structure",
        schema=schema,
    )

    dates = structure.get("dates") or {}
    classes = structure.get("classes") or []

    required_days = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
    ]

    for day_name in required_days:
        if not dates.get(day_name):
            raise RuntimeError(
                f"Could not confidently read the printed date for {day_name}."
            )

    if (
        len(classes) != 7
        or len(set(classes)) != 7
        or any(c not in PLANNER_SUBJECTS for c in classes)
    ):
        raise RuntimeError(
            "Could not confidently map the seven class rows. "
            "Please correct the row order below."
        )

    return {
        "dates": dates,
        "classes": classes,
    }



def crop_planner_cell(
    uploaded_file: Any,
    row_index: int,
    day_name: str,
) -> Image.Image:
    img = normalized_planner_image(uploaded_file)

    y1 = PLANNER_ROW_BOUNDS_Y[row_index]
    y2 = PLANNER_ROW_BOUNDS_Y[row_index + 1]
    x1, x2 = PLANNER_DAY_X[day_name]

    cell = crop_fraction(
        img,
        x1 + 0.004,
        y1 + 0.004,
        x2 - 0.004,
        y2 - 0.004,
    )

    # Enlarge handwriting for the transcription model.
    return cell.resize(
        (cell.width * 4, cell.height * 4),
        Image.Resampling.LANCZOS,
    )


def planner_transcription_prompt(
    class_name: str,
    day_name: str,
    due_date: str,
) -> str:
    return f"""
You are transcribing handwriting from ONE planner cell that the user has
already confirmed contains a real assignment.

Known class: {class_name}
Known day: {day_name}
Known date: {due_date}

Your task is TRANSCRIPTION, not interpretation.

Return ONLY valid JSON:
{{
  "transcription": string,
  "assignment_type": "Homework" | "Quiz" | "Test" | "Project" | "Reading" | "Essay" | "Other",
  "uncertainty_notes": string or null
}}

Rules:
- Copy only words that are visibly written in this cell.
- Do not paraphrase.
- Do not add instructions that are not visible.
- Do not invent context, topics, chapters, labs, narratives, projects, or
  descriptions.
- Ignore crossed-out or cancelled writing.
- If part of the handwriting is unreadable, use [unclear] for only that part.
- If the visible writing says "quiz unit 1", return transcription
  "quiz unit 1" — do not expand it.
- If the visible writing says "vocab quiz", return transcription
  "vocab quiz" — do not add study instructions.
- assignment_type should be inferred only from visible words such as
  quiz, test, reading, essay, project. Otherwise use Other.
- Never change the class or date.
""".strip()


def transcribe_selected_planner_cell(
    uploaded_file: Any,
    row_index: int,
    class_name: str,
    day_name: str,
    due_date: str,
) -> Dict[str, Any]:
    cell = crop_planner_cell(
        uploaded_file=uploaded_file,
        row_index=row_index,
        day_name=day_name,
    )

    raw = call_openai_images(
        planner_transcription_prompt(
            class_name=class_name,
            day_name=day_name,
            due_date=due_date,
        ),
        [pil_image_to_jpeg_bytes(cell)],
    )
    result = parse_json_from_text(raw)

    transcription = str(result.get("transcription") or "").strip()
    if not transcription:
        raise RuntimeError(
            f"Could not transcribe {class_name} on {day_name}."
        )

    assignment_type = result.get("assignment_type") or "Other"

    return {
        "class_name": class_name,
        "title": transcription,
        "description": "",
        "due_date": due_date,
        "due_time": None,
        "assignment_type": assignment_type,
        "estimated_effort_minutes": None,
        "priority": (
            "High"
            if assignment_type in {"Quiz", "Test", "Project", "Essay"}
            else "Normal"
        ),
        "materials_needed": None,
        "uncertainty_notes": result.get("uncertainty_notes"),
        "evidence": f"User-selected {class_name} / {day_name} planner cell.",
    }


def assignment_extraction_prompt() -> str:
    return f"""
You are helping a high school student capture assignments from a photo.

The image may contain:
- a single assignment
- a weekly student planner or agenda
- a classroom board
- an assignment sheet
- a syllabus
- a homework screenshot
- handwritten notes

IMPORTANT:
- The image may be rotated or sideways. Read it in the correct orientation.
- A planner may contain MULTIPLE assignments across different classes and dates.
- Extract EVERY assignment you can reasonably identify.
- Use the planner's row/class labels and column/date labels to associate each assignment with the correct class and due date.
- Handwriting may be difficult to read. Do NOT guess unclear words or dates.
- If something is uncertain, capture what you can and explain the uncertainty.

Return ONLY valid JSON with this exact structure:

{{
  "assignments": [
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
  ]
}}

Rules:
- Today is {date.today().isoformat()}.
- due_date must use YYYY-MM-DD.
- Pay close attention to printed planner dates.
- Treat each separate assignment, quiz, test, reading, essay, or project as a separate item.
- Do not create assignments from class names alone.
- Do not invent missing class names, dates, titles, or instructions.
- If handwriting is only partly legible, preserve the legible portion and explain the problem in uncertainty_notes.
- Use High priority for tests, quizzes, major projects, essays, or anything due within 24 hours.
- estimated_effort_minutes should be a practical student estimate only when reasonably supported; otherwise use null.
- If no assignments can be confidently identified, return {{"assignments": []}}.
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



def assignment_material_source(assignment_id: str, source_kind: str) -> str:
    return f"assignment:{assignment_id}:{source_kind}"


def load_assignment_study_materials(assignment_id: str) -> pd.DataFrame:
    """
    Reuse the existing study_materials table without a database migration.
    Assignment linkage is stored inside source_type.
    """
    materials = load_study_materials()

    if materials.empty or "source_type" not in materials.columns:
        return pd.DataFrame()

    prefix = f"assignment:{assignment_id}:"
    mask = (
        materials["source_type"]
        .fillna("")
        .astype(str)
        .str.startswith(prefix)
    )

    return materials[mask].copy()


def assignment_study_prompt(
    assignment: pd.Series,
    output_type: str,
) -> str:
    return f"""
You are helping a high school student prepare for ONE specific assignment.

ASSIGNMENT:
Class: {assignment.get('class_name') or 'Unknown'}
Title: {assignment.get('title') or 'Assignment'}
Type: {assignment.get('assignment_type') or 'Assignment'}
Due date: {assignment.get('due_date') or 'Unknown'}

You will receive a photo of class notes, a study guide, worksheet, textbook
page, or teacher handout that belongs to this assignment.

Create a student-friendly {output_type}.

Use ONLY information visible in the uploaded material.
Do not fill gaps with outside knowledge.

For "study guide":
- Give a concise summary.
- Organize the important ideas by topic.
- Include important terms and simple definitions.
- End with "What to study first" and 3-5 priorities.

For "flashcards":
- Create 10-15 cards.
- Use:
  Q: ...
  A: ...

For "practice quiz":
- Create 8-12 questions.
- Mix recall and application when supported.
- Put the answer key at the end.

For "summary":
- Produce a clean, concise summary in plain language.

If the material is incomplete or unclear, say what needs to be checked.
Keep the result calm, practical, and not overwhelming.
""".strip()


def render_locked_in_study_tools(locked: pd.Series) -> None:
    st.markdown("### Study")
    st.caption(
        "Add class notes, study guides, worksheets, or handouts for this assignment."
    )

    materials = load_assignment_study_materials(str(locked["id"]))

    if not materials.empty:
        st.markdown(f"**Saved study materials: {len(materials)}**")

        for _, material in materials.head(8).iterrows():
            label = material.get("topic") or "Study material"

            with st.expander(str(label)):
                if material.get("image_path"):
                    display_stored_image(
                        material.get("image_path"),
                        caption="Source material",
                    )

                st.markdown(
                    material.get("generated_markdown") or ""
                )

    upload = st.file_uploader(
        "Add a picture of notes or study material",
        type=["png", "jpg", "jpeg", "webp"],
        key=f"locked_study_upload_{locked['id']}",
    )

    output_type = st.selectbox(
        "Create",
        ["study guide", "flashcards", "practice quiz", "summary"],
        key=f"locked_study_type_{locked['id']}",
    )

    if upload is not None:
        display_uploaded_image(upload, "Study source")

        if st.button(
            f"Create {output_type}",
            type="primary",
            key=f"locked_study_generate_{locked['id']}",
        ):
            if not ai_is_ready():
                st.error("AI is not configured.")
                return

            with st.spinner(f"Creating {output_type}..."):
                try:
                    generated = call_openai_image(
                        assignment_study_prompt(
                            locked,
                            output_type,
                        ),
                        upload,
                    )

                    image_path = upload_image_to_storage(
                        upload,
                        folder="study",
                    )

                    add_study_material(
                        {
                            "class_name": locked.get("class_name"),
                            "topic": (
                                f"{locked.get('title') or 'Assignment'} — "
                                f"{output_type.title()}"
                            ),
                            "source_type": assignment_material_source(
                                str(locked["id"]),
                                "photo",
                            ),
                            "original_text": (
                                f"Study material for assignment "
                                f"{locked.get('title') or locked['id']}"
                            ),
                            "generated_markdown": generated,
                            "image_path": image_path,
                        }
                    )

                    st.success(
                        f"{output_type.title()} created and saved."
                    )
                    st.rerun()

                except Exception as exc:
                    st.error(
                        f"I could not create the study material: {exc}"
                    )



def build_assignment_study_context(locked: pd.Series) -> str:
    parts = [
        "ASSIGNMENT",
        f"Class: {locked.get('class_name') or 'Unknown'}",
        f"Title: {locked.get('title') or 'Assignment'}",
        f"Type: {locked.get('assignment_type') or 'Assignment'}",
        f"Due date: {locked.get('due_date') or 'Unknown'}",
    ]

    materials = load_assignment_study_materials(str(locked["id"]))

    if not materials.empty:
        parts.append("\nSAVED STUDY MATERIALS")

        for i, (_, material) in enumerate(materials.head(12).iterrows(), start=1):
            parts.append(
                f"\n--- Material {i}: {material.get('topic') or 'Study material'} ---"
            )
            generated = str(material.get("generated_markdown") or "").strip()
            if generated:
                parts.append(generated)

    return "\n".join(parts)


def ask_locked_in_followup(
    locked: pd.Series,
    question: str,
    history: List[Dict[str, str]],
    extra_image: Optional[Any] = None,
) -> str:
    client = openai_client()
    if client is None:
        raise RuntimeError("OpenAI API key is not configured.")

    study_context = build_assignment_study_context(locked)
    recent_history = history[-8:]

    conversation_text = []
    for message in recent_history:
        role = message.get("role", "user")
        content = message.get("content", "")
        conversation_text.append(f"{role.upper()}: {content}")

    prompt = f"""
You are Locked In, an AI study coach for a high school student.

Answer follow-up questions about ONE assignment using the assignment context
and saved study materials below.

Rules:
- Ground answers in the provided study materials whenever possible.
- If the student has attached a new image, use it as additional context.
- Do not invent facts that are not supported by the materials or image.
- If the materials do not contain enough information, say that clearly.
- If the student is asking about a math or homework problem, walk through the
  solution step by step in a helpful, student-friendly way.
- Keep answers concise, clear, and encouraging.

CONTEXT:
{study_context}

RECENT CONVERSATION:
{chr(10).join(conversation_text)}

NEW QUESTION:
{question}
""".strip()

    content = [{"type": "input_text", "text": prompt}]

    if extra_image is not None:
        data_url, _, _ = image_to_data_url(extra_image)
        content.append({"type": "input_image", "image_url": data_url, "detail": "high"})

    response = client.responses.create(
        model=DEFAULT_MODEL,
        input=[
            {
                "role": "user",
                "content": content,
            }
        ],
    )
    return response.output_text


def render_locked_in_followup_chat(locked: pd.Series) -> None:
    st.markdown("### Ask Locked In")
    st.caption(
        "Ask follow-up questions about this assignment or the study materials."
    )

    chat_key = f"locked_chat_{locked['id']}"
    if chat_key not in st.session_state:
        st.session_state[chat_key] = []

    history = st.session_state[chat_key]

    if history:
        for message in history:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
    else:
        st.info(
            "Try: “Explain this in simpler terms,” “Quiz me on this,” "
            "“How do I solve #4?” or “What should I study first?”"
        )

    with st.container(border=True):
        st.markdown("#### Ask a question")

        typed_question = st.text_area(
            "Type your question (optional)",
            key=f"locked_followup_text_{locked['id']}",
            placeholder="Example: How do I solve number 4?",
        )

        voice_question = st.audio_input(
            "Or record your question",
            key=f"locked_followup_audio_{locked['id']}",
        )

        st.caption("Optional: add another picture of the exact problem.")
        photo_source = st.radio(
            "Problem photo",
            ["None", "Camera", "Upload"],
            horizontal=True,
            key=f"locked_followup_photo_source_{locked['id']}",
        )

        extra_image = None
        if photo_source == "Camera":
            extra_image = st.camera_input(
                "Take a picture of the problem",
                key=f"locked_followup_camera_{locked['id']}",
            )
        elif photo_source == "Upload":
            extra_image = st.file_uploader(
                "Upload a picture of the problem",
                type=["png", "jpg", "jpeg", "webp"],
                key=f"locked_followup_upload_{locked['id']}",
            )

        if extra_image is not None:
            display_uploaded_image(extra_image, "Problem photo")

        if st.button(
            "Ask Locked In",
            type="primary",
            key=f"locked_followup_submit_{locked['id']}",
        ):
            try:
                question = typed_question.strip()

                if voice_question is not None:
                    transcribed = transcribe_voice_note(voice_question)
                    st.session_state[
                        f"locked_followup_last_transcript_{locked['id']}"
                    ] = transcribed

                    if not question:
                        question = transcribed

                if not question:
                    st.error("Please type or record a question.")
                else:
                    history.append({"role": "user", "content": question})

                    with st.chat_message("user"):
                        st.markdown(question)
                        if extra_image is not None:
                            st.caption("Attached a problem photo.")

                    with st.chat_message("assistant"):
                        with st.spinner("Thinking..."):
                            answer = ask_locked_in_followup(
                                locked=locked,
                                question=question,
                                history=history,
                                extra_image=extra_image,
                            )
                            st.markdown(answer)

                    history.append({"role": "assistant", "content": answer})
                    st.session_state[chat_key] = history

                    # Clear the typed question after sending.
                    st.session_state[f"locked_followup_text_{locked['id']}"] = ""

            except Exception as exc:
                st.error(f"I couldn't answer that question: {exc}")

    transcript_key = f"locked_followup_last_transcript_{locked['id']}"
    if st.session_state.get(transcript_key):
        st.caption(
            f"Last voice question heard: {st.session_state[transcript_key]}"
        )


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
    st.title(APP_NAME)
    st.caption("One place for assignments, due dates, and study help.")
    st.caption(f"Build: {APP_VERSION}")


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

def assignment_priority_score(row: pd.Series) -> float:
    """
    Simple first-pass recommendation score for Today.
    Higher score = more urgent / more important.
    """
    score = 0.0
    due = parse_iso_date(row.get("due_date"))
    today = date.today()

    if due:
        days = (due - today).days
        if days < 0:
            score += 100
        elif days == 0:
            score += 80
        elif days == 1:
            score += 65
        elif days <= 3:
            score += 45
        elif days <= 7:
            score += 25

    assignment_type = str(row.get("assignment_type") or "").lower()
    if assignment_type == "test":
        score += 30
    elif assignment_type == "quiz":
        score += 22
    elif assignment_type in {"essay", "project"}:
        score += 18

    priority = str(row.get("priority") or "").lower()
    if priority == "high":
        score += 20
    elif priority == "low":
        score -= 5

    if row.get("status") == "In progress":
        score += 12

    return score


def choose_recommended_assignment(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        raise ValueError("No assignments available.")
    scored = df.copy()
    scored["_locked_in_score"] = scored.apply(
        assignment_priority_score,
        axis=1,
    )
    scored = scored.sort_values(
        by=["_locked_in_score", "due_date"],
        ascending=[False, True],
        na_position="last",
    )
    return scored.iloc[0]


def ask_study_helper(
    assignment: Optional[pd.Series],
    question: str,
    mode: str,
    image: Optional[Any] = None,
) -> str:
    client = openai_client()
    if client is None:
        raise RuntimeError("OpenAI API key is not configured.")

    assignment_context = "No specific assignment selected."
    learning_profile = ""

    if assignment is not None:
        assignment_context = (
            f"Class: {assignment.get('class_name') or 'Unknown'}\\n"
            f"Assignment: {assignment.get('title') or 'Assignment'}\\n"
            f"Type: {assignment.get('assignment_type') or 'Assignment'}\\n"
            f"Due: {assignment.get('due_date') or 'Unknown'}"
        )
        learning_profile = build_learning_profile_context(
            str(assignment.get("class_name") or "")
        )

    mode_rules = {
        "Explain / answer a question": (
            "Answer the student's question clearly. For math, show the steps "
            "and explain why each step works."
        ),
        "Give me a hint": (
            "Do not give the full answer immediately. Give one useful hint, "
            "then a next step the student can try."
        ),
        "Walk me through it": (
            "Teach the problem step by step. Ask the student to think through "
            "key steps when useful, but provide enough guidance to keep moving."
        ),
        "Check my work": (
            "Inspect the student's work carefully. Identify which answers appear "
            "correct and which appear incorrect. For any incorrect item, explain "
            "the first point where the work goes wrong and show how to correct it. "
            "Do not claim you checked something that is not visible."
        ),
    }

    prompt = f"""
You are Locked In, an AI study coach helping a high school student with
homework right now.

ASSIGNMENT CONTEXT:
{assignment_context}

{learning_profile}

MODE:
{mode}

STUDENT QUESTION:
{question or "The student attached a homework image and wants help."}

INSTRUCTIONS:
{mode_rules.get(mode, mode_rules["Explain / answer a question"])}

General rules:
- Use the attached image as the primary source when one is provided.
- Read the actual problem and the student's visible work carefully.
- Do not invent numbers, instructions, questions, or answers that are not visible.
- If part of the image cannot be read, say exactly what is unclear.
- For algebra/math, preserve the problem exactly and show mathematically valid steps.
- Keep the response practical and concise enough to use while doing homework.
""".strip()

    content = [{"type": "input_text", "text": prompt}]

    if image is not None:
        data_url, _, _ = image_to_data_url(image)
        content.append(
            {
                "type": "input_image",
                "image_url": data_url,
                "detail": "high",
            }
        )

    response = client.responses.create(
        model=DEFAULT_MODEL,
        input=[
            {
                "role": "user",
                "content": content,
            }
        ],
    )
    return response.output_text


def render_study_question_helper(
    assignment: Optional[pd.Series],
    key_prefix: str,
) -> None:
    st.markdown("### Ask about homework")
    st.caption(
        "Type or record a question, and optionally take a picture of the exact "
        "problem or your completed work."
    )

    mode = st.selectbox(
        "What do you want help with?",
        [
            "Explain / answer a question",
            "Give me a hint",
            "Walk me through it",
            "Check my work",
        ],
        key=f"{key_prefix}_mode",
    )

    typed = st.text_area(
        "Type a question (optional)",
        placeholder="Example: Why did I get #6 wrong?",
        key=f"{key_prefix}_typed",
    )

    voice = st.audio_input(
        "Or record your question",
        key=f"{key_prefix}_voice",
    )

    st.caption(
        "For small equations or handwriting, Upload from Photos gives the best quality."
    )

    photo_source = st.radio(
        "Add a problem or homework picture",
        ["None", "Upload", "Camera"],
        horizontal=True,
        key=f"{key_prefix}_photo_source",
    )

    image = None

    if photo_source == "Camera":
        image = st.camera_input(
            "Take a picture",
            key=f"{key_prefix}_camera",
        )
    elif photo_source == "Upload":
        image = st.file_uploader(
            "Upload a homework picture",
            type=["png", "jpg", "jpeg", "webp"],
            key=f"{key_prefix}_upload",
        )

    if image is not None:
        display_uploaded_image(image, "Homework / problem")

    if st.button(
        "Ask Locked In",
        type="primary",
        key=f"{key_prefix}_submit",
    ):
        try:
            question = typed.strip()

            if voice is not None:
                transcript = transcribe_voice_note(voice)
                if not question:
                    question = transcript
                st.caption(f"I heard: {transcript}")

            if not question and image is None:
                st.error("Please type/record a question or add a picture.")
                return

            with st.spinner("Helping with the homework..."):
                answer = ask_study_helper(
                    assignment=assignment,
                    question=question,
                    mode=mode,
                    image=image,
                )

            st.session_state[f"{key_prefix}_last_answer"] = answer

        except Exception as exc:
            st.error(f"I couldn't help with that yet: {exc}")

    last_answer = st.session_state.get(
        f"{key_prefix}_last_answer"
    )
    if last_answer:
        st.markdown("#### Locked In")
        st.markdown(last_answer)


def page_today() -> None:
    st.subheader("What should I work on?")
    df = load_assignments(include_done=False)

    if df.empty:
        st.markdown(
            """
            <div class="metric-card">
              <strong>No open assignments yet.</strong><br>
              <span class="small-muted">Use Add to enter an assignment.</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    recommended = choose_recommended_assignment(df)

    st.markdown("### 🔒 Recommended")
    st.caption("Locked In's best suggestion based on due date and importance.")
    assignment_card(recommended, show_actions=False)

    assignment_options = {
        f"{row.get('class_name') or 'Class'} — {row.get('title') or 'Assignment'}"
        f" — {due_label(row.get('due_date'), row.get('due_time'))}": str(row["id"])
        for _, row in df.iterrows()
    }

    recommended_label = next(
        (
            label
            for label, assignment_id in assignment_options.items()
            if assignment_id == str(recommended["id"])
        ),
        list(assignment_options.keys())[0],
    )

    st.markdown("#### Choose what you want to work on")
    chosen_label = st.selectbox(
        "Assignment",
        list(assignment_options.keys()),
        index=list(assignment_options.keys()).index(recommended_label),
        key="today_lockin_choice",
        label_visibility="collapsed",
    )

    chosen_id = assignment_options[chosen_label]
    chosen_rows = df[df["id"].astype(str) == chosen_id]
    chosen = chosen_rows.iloc[0]

    if st.button(
        f"🔒 Lock In: {chosen.get('class_name') or 'Assignment'}",
        key=f"lockin_{chosen_id}",
        type="primary",
    ):
        update_assignment_status(
            chosen["id"],
            "In progress",
        )
        st.session_state[
            "locked_in_assignment_id"
        ] = chosen["id"]
        st.rerun()

    locked_id = st.session_state.get(
        "locked_in_assignment_id"
    )

    if locked_id:
        locked_rows = df[
            df["id"].astype(str) == str(locked_id)
        ]

        if not locked_rows.empty:
            locked = locked_rows.iloc[0]

            st.markdown("## 🔒 You're Locked In")
            st.write(f"**{locked.get('title')}**")
            st.write(
                f"{locked.get('class_name') or 'No class'} • "
                f"{locked.get('assignment_type') or 'Assignment'}"
            )
            st.write(
                f"Estimated time: "
                f"{locked.get('estimated_effort_minutes') or 30} minutes"
            )

            render_locked_in_study_tools(locked)
            render_locked_in_followup_chat(locked)

            col_done, col_pause = st.columns(2)

            with col_done:
                if st.button(
                    "✅ I'm Done",
                    key=f"finish_{locked['id']}",
                ):
                    update_assignment_status(
                        locked["id"],
                        "Done",
                    )
                    st.session_state.pop(
                        "locked_in_assignment_id",
                        None,
                    )
                    st.rerun()

            with col_pause:
                if st.button(
                    "Pause",
                    key=f"pause_{locked['id']}",
                ):
                    st.session_state.pop(
                        "locked_in_assignment_id",
                        None,
                    )
                    st.rerun()

    today = date.today()
    df["due_date_parsed"] = df["due_date"].apply(
        parse_iso_date
    )

    overdue = df[
        df["due_date_parsed"].notna()
        & (df["due_date_parsed"] < today)
    ]

    today_df = df[
        df["due_date_parsed"] == today
    ]

    week_df = df[
        df["due_date_parsed"].notna()
        & (df["due_date_parsed"] > today)
        & (
            df["due_date_parsed"]
            <= today + timedelta(days=7)
        )
    ]

    no_date = df[
        df["due_date_parsed"].isna()
    ]

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



def clear_assignment_capture_state() -> None:
    for key in [
        "last_assignment_extracts",
        "last_uploaded_file",
        "planner_upload_signature",
        "planner_last_structure",
    ]:
        st.session_state.pop(key, None)


def uploaded_file_signature(uploaded_file: Any) -> Optional[str]:
    if uploaded_file is None:
        return None
    try:
        return hashlib.sha256(uploaded_file.getvalue()).hexdigest()
    except Exception:
        return None



def page_add_assignment() -> None:
    st.subheader("Add Assignment")
    render_ai_notice()

    method = st.radio(
        "How do you want to update school?",
        ["🎙️ Tell Locked In", "Quick Add", "Photo"],
        horizontal=True,
    )

    if method == "🎙️ Tell Locked In":
        render_tell_locked_in()
        return

    if method == "Photo":
        photo_mode = st.radio(
            "What are you photographing?",
            ["Weekly planner", "Single assignment"],
            horizontal=True,
        )

        capture_mode = st.radio(
            "Photo source",
            ["Camera", "Upload"],
            horizontal=True,
        )

        if capture_mode == "Camera":
            uploaded = st.camera_input("Take a picture")
        else:
            uploaded = st.file_uploader(
                "Upload image",
                type=["png", "jpg", "jpeg", "webp"],
            )

        if uploaded is not None:
            current_signature = uploaded_file_signature(uploaded)
            prior_signature = st.session_state.get("planner_upload_signature")

            if prior_signature and current_signature != prior_signature:
                clear_assignment_capture_state()
                st.session_state.pop("planner_structure", None)

            st.session_state["planner_upload_signature"] = current_signature
            display_uploaded_image(uploaded, "Assignment source")

            # -----------------------------
            # WEEKLY PLANNER
            # -----------------------------
            if photo_mode == "Weekly planner":
                st.caption(
                    "Scan the planner, then tap only the boxes that contain "
                    "real assignments."
                )

                if ai_is_ready():
                    if st.button("Scan planner", type="primary"):
                        clear_assignment_capture_state()
                        st.session_state.pop("planner_structure", None)

                        with st.spinner("Reading dates and class rows..."):
                            try:
                                structure = read_planner_structure(uploaded)
                                st.session_state["planner_structure"] = structure
                                st.session_state["last_uploaded_file"] = uploaded
                            except Exception as exc:
                                st.error(f"I could not scan the planner: {exc}")
                else:
                    st.warning(
                        "Add OPENAI_API_KEY in secrets to read the planner."
                    )

                structure = st.session_state.get("planner_structure")

                if structure:
                    dates = structure["dates"]
                    detected_classes = structure["classes"]

                    # Default to the detected order.
                    confirmed_classes = list(detected_classes)

                    # Keep corrections available, but out of the main flow.
                    with st.expander("Class rows look wrong? Fix them here"):
                        st.caption(
                            "Only change these if Locked In put a subject "
                            "on the wrong row."
                        )

                        corrected = []

                        for row_index in range(7):
                            detected = detected_classes[row_index]
                            default_index = (
                                PLANNER_SUBJECTS.index(detected)
                                if detected in PLANNER_SUBJECTS
                                else row_index
                            )

                            subject = st.selectbox(
                                f"Row {row_index + 1}",
                                PLANNER_SUBJECTS,
                                index=default_index,
                                key=f"planner_subject_row_{row_index}",
                            )
                            corrected.append(subject)

                        if len(set(corrected)) == 7:
                            confirmed_classes = corrected
                        else:
                            st.warning(
                                "Each subject should appear once. "
                                "Using the detected row order until fixed."
                            )

                    st.markdown("### Tap the boxes with assignments")

                    required_days = [
                        "Monday",
                        "Tuesday",
                        "Wednesday",
                        "Thursday",
                        "Friday",
                    ]

                    selected_cells = []

                    # Each checkbox carries its own weekday/date label so the
                    # user never has to visually map a checkbox to a header.
                    for row_index, class_name in enumerate(confirmed_classes):
                        st.markdown(f"**{class_name}**")
                        row_cols = st.columns(5)

                        for day_index, day_name in enumerate(required_days):
                            due = parse_iso_date(dates[day_name])
                            date_label = (
                                due.strftime("%a %m/%d")
                                if due
                                else day_name[:3]
                            )

                            with row_cols[day_index]:
                                selected = st.checkbox(
                                    date_label,
                                    key=f"planner_cell_{row_index}_{day_name}",
                                )

                                if selected:
                                    selected_cells.append(
                                        {
                                            "row_index": row_index,
                                            "class_name": class_name,
                                            "day_name": day_name,
                                            "due_date": dates[day_name],
                                        }
                                    )

                        st.markdown("---")

                    if selected_cells:
                        st.caption(
                            f"{len(selected_cells)} assignment "
                            f"box(es) selected."
                        )

                        if st.button(
                            "Read selected assignments",
                            type="primary",
                        ):
                            st.session_state.pop(
                                "last_assignment_extracts",
                                None,
                            )

                            extracted = []

                            with st.spinner(
                                "Reading the selected boxes..."
                            ):
                                for cell_info in selected_cells:
                                    try:
                                        item = transcribe_selected_planner_cell(
                                            uploaded_file=uploaded,
                                            row_index=cell_info["row_index"],
                                            class_name=cell_info["class_name"],
                                            day_name=cell_info["day_name"],
                                            due_date=cell_info["due_date"],
                                        )
                                        extracted.append(item)
                                    except Exception as exc:
                                        extracted.append(
                                            {
                                                "class_name": cell_info[
                                                    "class_name"
                                                ],
                                                "title": "[Please enter]",
                                                "description": "",
                                                "due_date": cell_info[
                                                    "due_date"
                                                ],
                                                "due_time": None,
                                                "assignment_type": "Other",
                                                "estimated_effort_minutes": None,
                                                "priority": "Normal",
                                                "materials_needed": None,
                                                "uncertainty_notes": str(exc),
                                                "evidence": (
                                                    "User-selected planner cell."
                                                ),
                                            }
                                        )

                            st.session_state[
                                "last_assignment_extracts"
                            ] = extracted
                            st.session_state[
                                "last_uploaded_file"
                            ] = uploaded

                            st.rerun()

            # -----------------------------
            # SINGLE ASSIGNMENT PHOTO
            # -----------------------------
            else:
                if ai_is_ready():
                    if st.button(
                        "Read assignment",
                        type="primary",
                    ):
                        clear_assignment_capture_state()

                        with st.spinner("Reading the assignment..."):
                            try:
                                raw = call_openai_image(
                                    assignment_extraction_prompt(),
                                    uploaded,
                                )
                                result = parse_json_from_text(raw)
                                assignments = result.get(
                                    "assignments",
                                    [],
                                )

                                st.session_state[
                                    "last_assignment_extracts"
                                ] = assignments
                                st.session_state[
                                    "last_uploaded_file"
                                ] = uploaded
                            except Exception as exc:
                                st.error(
                                    f"I could not read the assignment: {exc}"
                                )
                else:
                    st.warning(
                        "Add OPENAI_API_KEY in secrets to extract assignments."
                    )

        extracted_assignments = st.session_state.get(
            "last_assignment_extracts",
            [],
        )
        saved_upload = st.session_state.get(
            "last_uploaded_file",
            uploaded,
        )

        if extracted_assignments:
            st.markdown("### Review and save")

            reviewed_assignments = []

            for i, item in enumerate(extracted_assignments):
                class_name_value = item.get("class_name") or ""
                title_value = item.get("title") or ""
                default_due = parse_iso_date(item.get("due_date"))

                with st.container(border=True):
                    include = st.checkbox(
                        f"Include assignment {i + 1}",
                        value=True,
                        key=f"include_assignment_{i}",
                    )

                    st.markdown(
                        f"**{class_name_value or 'Class'}"
                        f" — {default_due.strftime('%a, %b %d') if default_due else 'Date unclear'}**"
                    )

                    title = st.text_input(
                        "Assignment",
                        value=title_value,
                        key=f"title_{i}",
                    )

                    col1, col2 = st.columns(2)

                    with col1:
                        assignment_type = st.selectbox(
                            "Type",
                            [
                                "Homework",
                                "Quiz",
                                "Test",
                                "Project",
                                "Reading",
                                "Essay",
                                "Other",
                            ],
                            index=type_index(
                                item.get("assignment_type")
                            ),
                            key=f"type_{i}",
                        )

                    with col2:
                        priority = st.selectbox(
                            "Priority",
                            ["Low", "Normal", "High"],
                            index=priority_index(
                                item.get("priority")
                            ),
                            key=f"priority_{i}",
                        )

                    effort = st.number_input(
                        "Estimated minutes",
                        min_value=0,
                        max_value=600,
                        step=5,
                        value=safe_int(
                            item.get("estimated_effort_minutes")
                        ) or 30,
                        key=f"effort_{i}",
                    )

                    uncertainty = (
                        item.get("uncertainty_notes") or ""
                    )

                    if uncertainty:
                        st.warning(f"Please check: {uncertainty}")

                    reviewed_assignments.append(
                        {
                            "include": include,
                            "class_name": class_name_value,
                            "title": title,
                            "description": "",
                            "due_date": (
                                default_due.isoformat()
                                if isinstance(default_due, date)
                                else None
                            ),
                            "due_time": "",
                            "assignment_type": assignment_type,
                            "estimated_effort_minutes": int(
                                effort
                            ),
                            "priority": priority,
                            "status": "Not started",
                            "source": (
                                f"Photo: "
                                f"{getattr(saved_upload, 'name', 'planner photo')}"
                            ),
                            "uncertainty_notes": uncertainty,
                        }
                    )

            if st.button(
                "Save Selected Assignments",
                type="primary",
            ):
                selected = [
                    item
                    for item in reviewed_assignments
                    if item["include"]
                ]

                valid = [
                    item
                    for item in selected
                    if item["title"].strip()
                    and item["title"].strip()
                    != "[Please enter]"
                ]

                if not selected:
                    st.error(
                        "Select at least one assignment to save."
                    )

                elif len(valid) != len(selected):
                    st.error(
                        "One selected assignment still needs "
                        "its title corrected."
                    )

                else:
                    image_path = None

                    if saved_upload is not None:
                        with st.spinner(
                            "Saving planner photo..."
                        ):
                            image_path = (
                                upload_image_to_storage(
                                    saved_upload,
                                    folder="assignments",
                                )
                            )

                    for item in valid:
                        item["image_path"] = image_path
                        item.pop("include", None)
                        add_assignment(item)

                    clear_assignment_capture_state()
                    st.session_state.pop(
                        "planner_structure",
                        None,
                    )

                    st.success(
                        f"Saved {len(valid)} assignment(s)."
                    )
                    st.rerun()

    # -----------------------------
    # MANUAL ENTRY
    # -----------------------------
    else:
        with st.form(
            "manual_assignment_form",
            clear_on_submit=True,
        ):
            st.markdown("### Quick Add")

            class_name = st.text_input("Class")
            title = st.text_input("Assignment title")
            description = st.text_area(
                "Description",
                height=90,
            )

            col1, col2 = st.columns(2)

            with col1:
                due_date_value = st.date_input(
                    "Due date",
                    value=None,
                    format="YYYY-MM-DD",
                )

            with col2:
                due_time = st.text_input(
                    "Due time",
                    placeholder="Example: 11:59 PM",
                )

            col3, col4 = st.columns(2)

            with col3:
                assignment_type = st.selectbox(
                    "Type",
                    [
                        "Homework",
                        "Quiz",
                        "Test",
                        "Project",
                        "Reading",
                        "Essay",
                        "Other",
                    ],
                )

            with col4:
                priority = st.selectbox(
                    "Priority",
                    ["Low", "Normal", "High"],
                    index=1,
                )

            effort = st.number_input(
                "Estimated minutes",
                min_value=0,
                max_value=600,
                step=5,
                value=30,
            )

            submitted = st.form_submit_button(
                "Save assignment",
                type="primary",
            )

            if submitted:
                if not title.strip():
                    st.error(
                        "Please add an assignment title "
                        "before saving."
                    )
                else:
                    add_assignment(
                        {
                            "class_name": class_name.strip(),
                            "title": title.strip(),
                            "description": description.strip(),
                            "due_date": (
                                due_date_value.isoformat()
                                if isinstance(
                                    due_date_value,
                                    date,
                                )
                                else None
                            ),
                            "due_time": due_time.strip(),
                            "assignment_type": assignment_type,
                            "estimated_effort_minutes": int(
                                effort
                            ),
                            "priority": priority,
                            "status": "Not started",
                            "source": "Manual entry",
                            "uncertainty_notes": "",
                            "image_path": None,
                        }
                    )

                    st.success("Assignment saved.")


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
    st.subheader("Study")
    render_ai_notice()

    homework_tab, review_tab = st.tabs(
        ["Homework & Study", "📄 Review a Test"]
    )

    with homework_tab:

        assignments = load_assignments(include_done=False)
        selected_assignment = None

        if not assignments.empty:
            options = ["No specific assignment"]

            option_to_id = {}

            for _, row in assignments.iterrows():
                label = (
                    f"{row.get('class_name') or 'Class'} — "
                    f"{row.get('title') or 'Assignment'} — "
                    f"{due_label(row.get('due_date'), row.get('due_time'))}"
                )
                options.append(label)
                option_to_id[label] = str(row["id"])

            selected_label = st.selectbox(
                "What are you working on?",
                options,
                key="study_assignment_choice",
            )

            if selected_label != "No specific assignment":
                assignment_id = option_to_id[selected_label]
                rows = assignments[
                    assignments["id"].astype(str) == assignment_id
                ]
                if not rows.empty:
                    selected_assignment = rows.iloc[0]

        render_study_question_helper(
            assignment=selected_assignment,
            key_prefix="study_help",
        )

        st.markdown("---")
        st.markdown("### Create study material")

        source_type = st.radio(
            "Add material by",
            ["Photo", "Text"],
            horizontal=True,
            key="study_material_source_type",
        )

        default_class = (
            str(selected_assignment.get("class_name") or "")
            if selected_assignment is not None
            else ""
        )
        default_topic = (
            str(selected_assignment.get("title") or "")
            if selected_assignment is not None
            else ""
        )

        class_name = st.text_input(
            "Class",
            value=default_class,
            placeholder="Example: Biology",
            key="study_class_name",
        )
        topic = st.text_input(
            "Topic",
            value=default_topic,
            placeholder="Example: Cell division",
            key="study_topic",
        )

        output_type = st.selectbox(
            "Create",
            [
                "complete study guide",
                "flashcards and quiz",
                "clean summary",
            ],
            key="study_output_type",
        )

        uploaded = None
        notes_text = ""

        if source_type == "Photo":
            capture_mode = st.radio(
                "Photo source",
                ["Camera", "Upload"],
                horizontal=True,
                key="study_photo_source",
            )

            if capture_mode == "Camera":
                uploaded = st.camera_input(
                    "Take a picture of notes, homework, or study guide",
                    key="study_camera",
                )
            else:
                uploaded = st.file_uploader(
                    "Upload notes/homework image",
                    type=["png", "jpg", "jpeg", "webp"],
                    key="study_upload",
                )

            if uploaded is not None:
                display_uploaded_image(
                    uploaded,
                    "Study material",
                )
        else:
            notes_text = st.text_area(
                "Paste notes here",
                height=220,
                key="study_notes_text",
            )

        if st.button(
            "Generate study help",
            type="primary",
            key="study_generate",
        ):
            if not ai_is_ready():
                st.error(
                    "Add OPENAI_API_KEY in Streamlit secrets "
                    "to generate study tools."
                )
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
                        generated = call_openai_image(
                            study_prompt_from_image(output_type),
                            uploaded,
                        )
                        original_text = (
                            f"Photo: "
                            f"{getattr(uploaded, 'name', 'camera image')}"
                        )
                        image_path = upload_image_to_storage(
                            uploaded,
                            folder="study",
                        )
                    else:
                        generated = call_openai_text(
                            study_prompt_from_text(
                                notes_text,
                                output_type,
                            )
                        )
                        original_text = notes_text

                    linked_source_type = source_type
                    if selected_assignment is not None:
                        linked_source_type = assignment_material_source(
                            str(selected_assignment["id"]),
                            source_type.lower(),
                        )

                    add_study_material(
                        {
                            "class_name": class_name.strip(),
                            "topic": topic.strip(),
                            "source_type": linked_source_type,
                            "original_text": original_text,
                            "generated_markdown": generated,
                            "image_path": image_path,
                        }
                    )

                    st.session_state[
                        "last_study_output"
                    ] = generated

                    st.success(
                        "Study material created and saved."
                    )

                except Exception as exc:
                    st.error(
                        f"I could not generate study materials: {exc}"
                    )

        if "last_study_output" in st.session_state:
            st.markdown("### Latest study output")
            st.markdown(
                st.session_state["last_study_output"]
            )

        st.markdown("### Saved study materials")
        materials = load_study_materials()

        if materials.empty:
            st.info("No saved study materials yet.")
        else:
            for _, row in materials.head(10).iterrows():
                with st.expander(
                    f"{row.get('class_name') or 'Class'} — "
                    f"{row.get('topic') or 'Study material'}"
                ):
                    if row.get("image_path"):
                        display_stored_image(
                            row.get("image_path"),
                            caption="Original photo",
                        )

                    st.markdown(
                        row.get("generated_markdown") or ""
                    )

                    st.download_button(
                        "Download as Markdown",
                        data=(
                            row.get("generated_markdown") or ""
                        ).encode("utf-8"),
                        file_name=(
                            f"study_material_{row.get('id')}.md"
                        ),
                        mime="text/markdown",
                        key=f"download_md_{row.get('id')}",
                    )



    with review_tab:
        render_test_review_tool()

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

    st.markdown("### My Classes")
    st.caption(
        "Set up your own classes. These will become the default choices for "
        "assignment entry and voice commands in the beta."
    )

    classes = load_my_classes()

    if classes:
        for class_name in classes:
            col_name, col_delete = st.columns([4, 1])
            col_name.write(f"**{class_name}**")
            with col_delete:
                if st.button(
                    "Remove",
                    key=f"remove_class_{class_name}",
                ):
                    delete_my_class(class_name)
                    st.rerun()
    else:
        st.info("No classes added yet.")

    with st.form("add_class_form", clear_on_submit=True):
        new_class = st.text_input(
            "Add a class",
            placeholder="e.g., Biology",
        )
        add_class = st.form_submit_button("Add class")

    if add_class and new_class.strip():
        add_my_class(new_class)
        st.rerun()

    st.markdown("### Account")
    st.caption(st.session_state.get("user_email", ""))

    if st.button("Sign out"):
        try:
            get_supabase().auth.sign_out()
        except Exception:
            pass
        clear_auth_state()
        st.rerun()

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
    if not render_auth_gate():
        return

    # Prevent stale extraction results from surviving a code redeploy.
    if st.session_state.get("capture_build_version") != APP_VERSION:
        clear_assignment_capture_state()
        st.session_state["capture_build_version"] = APP_VERSION

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
    
