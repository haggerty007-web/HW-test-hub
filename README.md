# Homework Hub

A mobile-first Streamlit app for tracking school assignments and turning class notes or study guides into study materials.

## What it does

- Add assignments manually
- Add assignments from photos/screenshots using OpenAI vision
- Review extracted assignment details before saving
- View overdue, today, and upcoming assignments
- Mark assignments as not started, in progress, or done
- Create study guides, flashcards, quizzes, and study plans from photos or pasted notes
- Export assignments as CSV
- Export assignments as an `.ics` calendar file
- Use from an iPhone by adding the Streamlit URL to the Home Screen

## Files

streamlit>=1.36
openai>=1.54.0
pandas>=2.2.0
pillow>=10.0.0
supabase>=2.4.0
```

## Local setup

1. Create a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Add your OpenAI API key:

```bash
mkdir -p .streamlit
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

Then edit `.streamlit/secrets.toml`:

```toml
OPENAI_API_KEY = "sk-your-key-here"
```

4. Run the app:

```bash
streamlit run app.py
```

## Deploy on Streamlit Community Cloud

1. Create a GitHub repository, for example `homework-hub`.
2. Upload `app.py`, `requirements.txt`, `README.md`, and `.streamlit/secrets.toml.example`.
3. Go to Streamlit Community Cloud and create a new app from that repo.
4. Main file path: `app.py`.
5. In the app's Secrets settings, add:

```toml
OPENAI_API_KEY = "sk-your-key-here"
```

6. Deploy.

## Use on iPhone

1. Open the deployed Streamlit app URL in Safari.
2. Tap the Share button.
3. Tap **Add to Home Screen**.
4. Name it **Homework Hub**.

## Important MVP note

This version uses a local SQLite database under `data/homework_hub.sqlite`. This is fine for testing and lightweight personal use, but Streamlit Community Cloud local storage is not the best long-term database. For a more durable version, connect the app to Google Sheets, Supabase, Firebase, or another hosted database.

## Suggested next upgrades

- Google Calendar sync
- Persistent hosted database
- Parent view
- Daily email/text reminder
- Class color coding
- Due-date confidence scoring
- Recurring assignments
- Attachment storage for source photos
