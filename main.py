import streamlit as st
import pandas as pd
from tinydb import TinyDB, Query, where
from tinydb.operations import set as tinydb_set
import json
from pathlib import Path
from datetime import datetime, date, timedelta
import shutil
import pytz # Required for timezone-aware datetime comparisons

# --- Constants & Configuration ---
APP_VERSION = "v2.0"
DB_FILE = Path("data/novel_forge_db.json")
SNAPSHOT_DIR = Path("data/snapshots")
DEMO_DATA_FILE = Path("demo_data.json")
MAX_SNAPSHOTS = 5
TARGET_DEADLINE = datetime(2025, 6, 1).date() # Approx June 1st
CSS_FILE = Path("assets/style.css")

# Ensure data directories exist
DB_FILE.parent.mkdir(parents=True, exist_ok=True)
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
CSS_FILE.parent.mkdir(parents=True, exist_ok=True)

# Kaela's potential snark (for the Easter Egg)
KAELA_QUOTES = [
    "Took you long enough, scribe.",
    "Right, what's next? Don't dawdle.",
    "Finally! Thought you'd never finish that bit.",
    "Acceptable. Barely.",
    "Progress! Or perhaps just... completion.",
]

# --- Database Setup (TinyDB) ---
db = TinyDB(DB_FILE, indent=4)
chapters_table = db.table('chapters')
editing_passes_table = db.table('editing_passes')
todos_table = db.table('todos')
metadata_table = db.table('metadata')

# --- Helper Functions ---

def load_css(file_path):
    """Loads custom CSS file."""
    try:
        with open(file_path) as f:
            st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)
    except FileNotFoundError:
        st.warning(f"CSS file not found at {file_path}. Using default styles.")
        # Create a basic CSS file if it doesn't exist
        basic_css = """
/* Basic placeholder CSS */
body { font-family: sans-serif; }
.stTabs [data-baseweb="tab-list"] button [data-testid="stMarkdownContainer"] p { font-size: 1rem; }
        """
        with open(file_path, "w") as f:
            f.write(basic_css)
        st.rerun() # Rerun to apply the newly created CSS


def get_local_now():
    """Gets the current time in the Chicago timezone."""
    # Note: Streamlit Cloud runs in UTC. For accurate *local* date comparisons
    # for deadlines/snapshots, we should ideally use a specific timezone.
    # Let's default to Chicago/Central time as per context.
    try:
        chicago_tz = pytz.timezone("America/Chicago")
        return datetime.now(chicago_tz)
    except pytz.exceptions.UnknownTimeZoneError:
        # Fallback if timezone database isn't available (less likely)
        return datetime.now()

def format_datetime(dt):
    """Formats datetime object for display."""
    if dt is None:
        return "N/A"
    if isinstance(dt, str): # Handle potential strings from DB
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return "Invalid Date"
    # Format like "Apr 29, 2025 10:15 AM"
    return dt.strftime("%b %d, %Y %I:%M %p") if dt else "N/A"

def calculate_countdown(deadline_str):
    """Calculates days remaining until the deadline."""
    if not deadline_str:
        return "N/A"
    try:
        deadline_date = datetime.strptime(deadline_str, "%Y-%m-%d").date()
        today = get_local_now().date()
        delta = deadline_date - today
        if delta.days < 0:
            return f"{abs(delta.days)} days OVERDUE"
        elif delta.days == 0:
            return "🔥 DUE TODAY"
        elif delta.days == 1:
             return "⚠️ 1 day left"
        else:
            return f"{delta.days} days left"
    except (ValueError, TypeError):
        return "Invalid Date"

def create_snapshot():
    """Creates a timestamped backup of the database file."""
    today_str = get_local_now().strftime("%Y-%m-%d")
    snapshot_file = SNAPSHOT_DIR / f"novel_forge_db_{today_str}.json"

    # Only create one snapshot per day
    if not snapshot_file.exists():
        try:
            shutil.copyfile(DB_FILE, snapshot_file)
            st.toast(f"Snapshot created: {snapshot_file.name}", icon="💾")
            # Prune old snapshots
            snapshots = sorted(SNAPSHOT_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old_snapshot in snapshots[MAX_SNAPSHOTS:]:
                old_snapshot.unlink()
                print(f"Deleted old snapshot: {old_snapshot.name}") # Log deletion
        except Exception as e:
            st.error(f"Failed to create snapshot: {e}")

def save_data(data_dict):
    """Saves all data tables back to TinyDB and creates a snapshot."""
    now_iso = get_local_now().isoformat()
    query = Query()

    # --- Metadata ---
    meta = data_dict.get('metadata', {})
    if metadata_table.contains(doc_id=1):
         metadata_table.update(meta, doc_ids=[1])
    else:
         metadata_table.insert({**meta, 'doc_id': 1}) # Ensure doc_id=1 for easy retrieval

    # --- Chapters ---
    saved_chapter_ids = set()
    for chapter in data_dict.get('chapters', []):
        chapter_id = chapter.get('id')
        if not chapter_id: continue # Should have an ID

        # Convert deadline back to string if it's a date object from date_input
        if isinstance(chapter.get('deadline'), date):
             chapter['deadline'] = chapter['deadline'].strftime('%Y-%m-%d')

        # Store last edited time if changed
        if chapter.get('_changed', False): # Check flag set during comparison
             chapter['last_edited'] = now_iso
             del chapter['_changed'] # Remove temporary flag

        # Handle potential NaNs from pandas/data_editor if WC is empty
        wc = chapter.get('word_count')
        chapter['word_count'] = int(wc) if pd.notna(wc) and wc is not None else 0

        # Update previous word count *before* saving the new one
        existing_chapter = chapters_table.get(doc_id=chapter_id)
        if existing_chapter and existing_chapter.get('word_count') != chapter['word_count']:
            chapter['previous_word_count'] = existing_chapter.get('word_count', 0)
        elif not existing_chapter: # New chapter
             chapter['previous_word_count'] = 0
        # else: word count unchanged, keep existing previous_word_count

        if chapters_table.contains(doc_id=chapter_id):
            chapters_table.update(chapter, doc_ids=[chapter_id])
        else:
            chapters_table.insert({**chapter}) # Add doc_id if not present? TinyDB handles it.
        saved_chapter_ids.add(chapter_id)

    # Remove chapters deleted via data_editor
    all_db_ids = {doc.doc_id for doc in chapters_table.all()}
    ids_to_remove = all_db_ids - saved_chapter_ids
    if ids_to_remove:
        chapters_table.remove(doc_ids=list(ids_to_remove))

    # --- Editing Passes ---
    saved_pass_ids = set()
    for edit_pass in data_dict.get('editing_passes', []):
        pass_id = edit_pass.get('id')
        if not pass_id: continue # Should have an ID

        # Link chapter_id correctly (might be None)
        # chapter_id_val = edit_pass.get('chapter_id')
        # edit_pass['chapter_id'] = int(chapter_id_val) if chapter_id_val else None

        if editing_passes_table.contains(doc_id=pass_id):
            editing_passes_table.update(edit_pass, doc_ids=[pass_id])
        else:
            editing_passes_table.insert({**edit_pass})
        saved_pass_ids.add(pass_id)

    all_db_pass_ids = {doc.doc_id for doc in editing_passes_table.all()}
    pass_ids_to_remove = all_db_pass_ids - saved_pass_ids
    if pass_ids_to_remove:
        editing_passes_table.remove(doc_ids=list(pass_ids_to_remove))


    # --- Todos ---
    saved_todo_ids = set()
    for todo in data_dict.get('todos', []):
        todo_id = todo.get('id')
        if not todo_id: continue # Should have an ID

        if todos_table.contains(doc_id=todo_id):
            todos_table.update(todo, doc_ids=[todo_id])
        else:
            todos_table.insert({**todo})
        saved_todo_ids.add(todo_id)

    all_db_todo_ids = {doc.doc_id for doc in todos_table.all()}
    todo_ids_to_remove = all_db_todo_ids - saved_todo_ids
    if todo_ids_to_remove:
        todos_table.remove(doc_ids=list(todo_ids_to_remove))

    # --- Create Snapshot ---
    create_snapshot()
    st.session_state.data_saved = True # Flag for confirmation


def load_data():
    """Loads data from TinyDB or initializes with demo data."""
    if not DB_FILE.exists() and DEMO_DATA_FILE.exists():
        st.info("Database not found. Loading demo data...")
        with open(DEMO_DATA_FILE, 'r') as f:
            demo_data = json.load(f)
        # Insert demo data into TinyDB, letting TinyDB assign doc_ids
        for item in demo_data.get('chapters', []):
            item.pop('id', None) # Remove demo ID if present
            chapters_table.insert(item)
        for item in demo_data.get('editing_passes', []):
            item.pop('id', None)
            editing_passes_table.insert(item)
        for item in demo_data.get('todos', []):
            item.pop('id', None)
            todos_table.insert(item)
        if 'metadata' in demo_data:
             if not metadata_table.contains(doc_id=1):
                metadata_table.insert({**demo_data['metadata'], 'doc_id': 1})
             else:
                 metadata_table.update(demo_data['metadata'], doc_ids=[1])
        # Reload from the newly created DB file
        # return load_data() # Recursive call after creating DB

    # Always load from DB, ensuring doc_id is added
    chapters = [{**doc, 'id': doc.doc_id} for doc in chapters_table.all()]
    editing_passes = [{**doc, 'id': doc.doc_id} for doc in editing_passes_table.all()]
    todos = [{**doc, 'id': doc.doc_id} for doc in todos_table.all()]
    metadata_list = metadata_table.all()
    metadata = metadata_list[0] if metadata_list else {'project_start_word_count': 0, 'target_word_count': 80000, 'dark_mode': False, 'doc_id': 1}

    # Convert deadline strings to date objects for date_input compatibility
    for chap in chapters:
        if chap.get('deadline'):
            try:
                chap['deadline_obj'] = datetime.strptime(chap['deadline'], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                chap['deadline_obj'] = None # Handle invalid date strings
        else:
             chap['deadline_obj'] = None
        # Ensure essential keys exist
        chap.setdefault('word_count', 0)
        chap.setdefault('previous_word_count', 0)
        chap.setdefault('status', 'Not Started')
        chap.setdefault('priority', '🟨 Low')


    return {
        'chapters': chapters,
        'editing_passes': editing_passes,
        'todos': todos,
        'metadata': metadata
    }

def get_next_id(table):
    """Gets the next available integer ID for a table."""
    max_id = 0
    for item in table.all():
        if item.doc_id > max_id:
            max_id = item.doc_id
    return max_id + 1

# --- Import Functions ---

def process_docx(uploaded_file):
    """Parses .docx file for chapters and word counts (placeholder)."""
    st.info("`.docx` import selected. Wiring this up requires `python-docx`.")
    st.code("# pip install python-docx", language="bash")
    # --- Placeholder logic ---
    # try:
    #     from docx import Document
    #     document = Document(uploaded_file)
    #     chapters_data = []
    #     current_chapter_title = "Chapter 1" # Default if no headings
    #     current_chapter_words = 0
    #     # Rough logic: Assume H1 or H2 are chapter titles
    #     for para in document.paragraphs:
    #         if para.style.name.startswith('Heading 1') or para.style.name.startswith('Heading 2'):
    #             if current_chapter_words > 0: # Save previous chapter
    #                  chapters_data.append({
    #                     "title": current_chapter_title,
    #                     "word_count": current_chapter_words,
    #                     "status": "Not Started",
    #                     "priority": "🟨 Low",
    #                     "deadline": None,
    #                     "previous_word_count": 0,
    #                     "last_edited": None
    #                  })
    #             current_chapter_title = para.text.strip() if para.text.strip() else f"Chapter {len(chapters_data) + 1}"
    #             current_chapter_words = 0
    #         else:
    #             current_chapter_words += len(para.text.split())

    #     # Add the last chapter
    #     if current_chapter_words > 0 or not chapters_data:
    #          chapters_data.append({
    #                 "title": current_chapter_title,
    #                 "word_count": current_chapter_words,
    #                 "status": "Not Started",
    #                 "priority": "🟨 Low",
    #                 "deadline": None,
    #                 "previous_word_count": 0,
    #                 "last_edited": None
    #          })

    #     st.success(f"Parsed {len(chapters_data)} potential chapters from {uploaded_file.name}.")
    #     return chapters_data

    # except ImportError:
    #     st.error("Please install `python-docx` to enable this feature.")
    #     return None
    # except Exception as e:
    #     st.error(f"Error processing .docx file: {e}")
    #     return None
    # --- End Placeholder ---
    return [] # Return empty list for now

def process_google_doc(url):
    """Fetches and parses Google Doc content (placeholder)."""
    st.info("Google Docs import selected. Wiring this up requires Google API setup.")
    st.code("# pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib", language="bash")
    st.warning("Requires setting up Google Cloud Credentials and OAuth flow.")
    # --- Placeholder logic ---
    # You would use the Google Docs API (docs.documents.get)
    # Parse the returned JSON structure (document.body.content)
    # Look for paragraph elements with specific heading styles (e.g., HEADING_1)
    # Calculate word counts similar to the docx logic.
    # --- End Placeholder ---
    return [] # Return empty list for now


# --- Streamlit App Layout ---

st.set_page_config(
    page_title=f"Novel-Forge Tracker {APP_VERSION}",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Apply custom CSS based on dark mode state
if 'app_data' not in st.session_state or st.session_state.app_data is None:
    st.session_state.app_data = load_data()
    st.session_state.data_loaded = True
    st.session_state.last_saved_state = json.dumps(st.session_state.app_data, sort_keys=True, default=str) # For change detection

# Handle dark mode toggle and CSS injection
dark_mode_enabled = st.session_state.app_data['metadata'].get('dark_mode', False)
if dark_mode_enabled:
    load_css(CSS_FILE) # Load full CSS which includes dark mode rules
else:
    # Inject a minimal style to potentially override dark mode if needed
     st.markdown("<style>:root { /* Light mode overrides if necessary */ }</style>", unsafe_allow_html=True)
    # Or just rely on Streamlit's default light theme

st.title(f"📚 Novel-Forge Tracker {APP_VERSION}")
st.caption(f"Editing Sprint Progress | Target Deadline: {TARGET_DEADLINE.strftime('%B %d, %Y')}")

# --- Sidebar ---
with st.sidebar:
    st.header("Dashboard")

    # Word Count Stats
    chapters_df = pd.DataFrame(st.session_state.app_data['chapters'])
    current_total_wc = chapters_df['word_count'].sum() if not chapters_df.empty else 0
    start_wc = st.session_state.app_data['metadata'].get('project_start_word_count', 0)
    delta_wc = current_total_wc - start_wc

    st.metric("Current Word Count", f"{current_total_wc:,}")
    st.metric("Total Change", f"{delta_wc:+,}", delta_color=("inverse" if delta_wc != 0 else "off"))

    # Target Word Count & Progress
    target_wc = st.number_input(
        "Target Word Count",
        min_value=0,
        value=st.session_state.app_data['metadata'].get('target_word_count', 80000),
        step=1000,
        key="target_wc_input",
        help="Set your overall manuscript word count goal."
    )
    # Update metadata immediately if changed
    if target_wc != st.session_state.app_data['metadata'].get('target_word_count', 80000):
        st.session_state.app_data['metadata']['target_word_count'] = target_wc
        # Autosave triggered later if other changes occur, or force save? Let's save.
        save_data(st.session_state.app_data)
        st.rerun()


    if target_wc > 0:
        progress = min(current_total_wc / target_wc, 1.0)
        st.progress(progress, text=f"{progress:.1%} towards target ({target_wc:,} words)")
    else:
        st.progress(0, text="Set a target word count")

    st.divider()

    # --- Import Wizard ---
    st.header("Import Chapters")
    import_method = st.radio("Import from:", (".docx file", "Google Doc URL"), horizontal=True, label_visibility="collapsed")

    imported_chapters = None
    if import_method == ".docx file":
        uploaded_file = st.file_uploader("Upload your manuscript", type=["docx"], key="docx_uploader")
        if uploaded_file:
            if st.button("Process .docx", key="process_docx_btn"):
                 with st.spinner("Parsing Word document..."):
                    imported_chapters = process_docx(uploaded_file)
    else:
        gdoc_url = st.text_input("Paste Google Doc URL", key="gdoc_url_input")
        if gdoc_url:
             if st.button("Process Google Doc", key="process_gdoc_btn"):
                 with st.spinner("Attempting to access Google Doc..."):
                     imported_chapters = process_google_doc(gdoc_url)

    if imported_chapters:
        # Clear existing chapters? Or append? Let's offer append/replace.
        import_action = st.radio("Import Action:", ("Replace existing chapters", "Append to existing chapters"), index=0)
        confirm_import = st.button("Confirm Import", type="primary")
        if confirm_import:
            new_chapter_list = []
            start_id = get_next_id(chapters_table) if import_action == "Append to existing chapters" else 1
            if import_action == "Append to existing chapters":
                 new_chapter_list.extend(st.session_state.app_data['chapters'])

            for i, chap_data in enumerate(imported_chapters):
                 new_chapter = {
                     'id': start_id + i,
                     'title': chap_data.get('title', f'Untitled Chapter {start_id + i}'),
                     'status': chap_data.get('status', 'Not Started'),
                     'word_count': chap_data.get('word_count', 0),
                     'previous_word_count': 0, # Always 0 on import
                     'priority': chap_data.get('priority', '🟨 Low'),
                     'deadline': chap_data.get('deadline'), # Keep as None or imported value
                     'last_edited': None # Never edited yet
                 }
                 # Convert deadline if needed
                 if isinstance(new_chapter.get('deadline'), date):
                     new_chapter['deadline'] = new_chapter['deadline'].strftime('%Y-%m-%d')
                 elif isinstance(new_chapter.get('deadline'), datetime):
                      new_chapter['deadline'] = new_chapter['deadline'].strftime('%Y-%m-%d')

                 new_chapter_list.append(new_chapter)

            st.session_state.app_data['chapters'] = new_chapter_list
            # Recalculate start word count if replacing
            if import_action == "Replace existing chapters":
                 st.session_state.app_data['metadata']['project_start_word_count'] = sum(c.get('word_count', 0) for c in new_chapter_list)
            st.success(f"Imported {len(imported_chapters)} chapters!")
            save_data(st.session_state.app_data) # Save imported data
            st.rerun() # Reload the UI with new data


    st.divider()

    # --- Settings ---
    st.header("Settings")
    # Dark Mode Toggle
    current_dark_mode = st.session_state.app_data['metadata'].get('dark_mode', False)
    new_dark_mode = st.toggle("🌙 Dark Mode", value=current_dark_mode, key="dark_mode_toggle")
    if new_dark_mode != current_dark_mode:
        st.session_state.app_data['metadata']['dark_mode'] = new_dark_mode
        save_data(st.session_state.app_data) # Save setting change
        st.rerun() # Rerun to apply CSS changes

    # --- Nice-to-Have Stubs ---
    st.divider()
    st.subheader("Extras (Coming Soon™)")
    st.button("🍅 Start Pomodoro Timer", disabled=True)
    st.caption("_(Pomodoro timer not implemented yet)_")

    st.text_input("Slack/Discord Webhook URL", disabled=True)
    st.caption("_(Deadline notification hooks not implemented yet)_")


# --- Main Content Area ---
tab1, tab2, tab3 = st.tabs(["📚 Chapters", "📝 Editing Passes", "✅ To-Do List"])

# --- Tab 1: Chapter Table ---
with tab1:
    st.header("Chapter Progress")

    # Prepare data for data_editor
    chapters_for_editor = []
    if 'chapters' in st.session_state.app_data:
        for i, chapter in enumerate(st.session_state.app_data['chapters']):
             chapters_for_editor.append({
                '#': i + 1, # Display index (not the ID)
                'Title': chapter.get('title', ' '), # Ensure non-null for editor
                'Status': chapter.get('status', 'Not Started'),
                'Word Count': chapter.get('word_count', 0),
                'Δ Words': chapter.get('word_count', 0) - chapter.get('previous_word_count', chapter.get('word_count', 0)), # Calculate Delta
                'Priority': chapter.get('priority', '🟨 Low'),
                'Deadline': chapter.get('deadline_obj'), # Use date obj for widget
                'Countdown': calculate_countdown(chapter.get('deadline')), # Calculate Countdown string
                'Last Edited': format_datetime(chapter.get('last_edited')),
                '_id': chapter['id'] # Hidden ID for tracking changes
            })

    # Configure columns for st.data_editor
    column_config = {
        "#": st.column_config.NumberColumn("Nr", width="small", disabled=True),
        "Title": st.column_config.TextColumn("Title", width="large", required=True),
        "Status": st.column_config.SelectboxColumn(
            "Status",
            options=["Not Started", "Draft", "Line-Edits", "✅ Done"],
            required=True,
        ),
        "Word Count": st.column_config.NumberColumn(
            "Words",
             min_value=0,
             step=10,
             required=True,
        ),
        "Δ Words": st.column_config.NumberColumn(
            "Δ", format="%+d", help="Change since last save", disabled=True
        ),
        "Priority": st.column_config.SelectboxColumn(
            "Prio",
            help="Editing Priority (High=Red)",
            width="small",
            options=["🟥 High", "🟧 Medium", "🟨 Low", "🟩 Optional"],
            required=True,
        ),
        "Deadline": st.column_config.DateColumn(
            "Deadline",
            min_value=date(2020, 1, 1),
            format="YYYY-MM-DD",
        ),
         "Countdown": st.column_config.TextColumn(
            "Countdown",
            help="Days remaining until deadline",
            disabled=True,
        ),
        "Last Edited": st.column_config.TextColumn(
            "Last Edited",
            disabled=True
        ),
        "_id": None # Hide internal ID column
    }

    # Display the editable data frame
    edited_chapters_df = st.data_editor(
        pd.DataFrame(chapters_for_editor),
        key="chapter_editor",
        column_config=column_config,
        num_rows="dynamic", # Allow adding/deleting rows
        hide_index=True,
        use_container_width=True,
        # on_change=handle_data_change # Using comparison method instead
    )

    # --- Detect Changes and Autosave for Chapters ---
    edited_chapters_list = edited_chapters_df.to_dict('records')
    current_app_state = []
    needs_save = False

    # Compare edited data with session state, handling potential type changes
    for edited_row in edited_chapters_list:
        original_chapter = next((c for c in st.session_state.app_data['chapters'] if c['id'] == edited_row['_id']), None)

        # Map editor row back to DB structure
        mapped_row = {
             'id': edited_row['_id'],
             'title': edited_row['Title'],
             'status': edited_row['Status'],
             'word_count': int(edited_row['Word Count']) if pd.notna(edited_row['Word Count']) else 0,
             'priority': edited_row['Priority'],
             'deadline': edited_row['Deadline'].strftime('%Y-%m-%d') if pd.notna(edited_row['Deadline']) and isinstance(edited_row['Deadline'], date) else None,
             '_changed': False # Flag for update detection
        }

        if original_chapter:
            # Compare relevant fields
            fields_to_compare = ['title', 'status', 'word_count', 'priority', 'deadline']
            for field in fields_to_compare:
                original_value = original_chapter.get(field)
                edited_value = mapped_row.get(field)

                # Handle date comparison (original is string, edited is date obj)
                if field == 'deadline' and isinstance(original_value, str) and isinstance(edited_value, date):
                     original_value = datetime.strptime(original_value, '%Y-%m-%d').date() if original_value else None


                if original_value != edited_value:
                    mapped_row['_changed'] = True
                    needs_save = True
                     # Nice-to-Have: Confetti trigger
                    if field == 'status' and edited_value == '✅ Done':
                        st.balloons()
                        # import random # Add import at top if using
                        # st.toast(f"Kaela says: \"{random.choice(KAELA_QUOTES)}\"", icon="🎉")
                    break # No need to check other fields if one changed
            # Keep existing unchanged fields
            mapped_row['previous_word_count'] = original_chapter.get('previous_word_count', original_chapter.get('word_count', 0)) # Preserve prev WC unless WC changes
            mapped_row['last_edited'] = original_chapter.get('last_edited') # Preserve last edited unless changed flag set
            current_app_state.append({**original_chapter, **mapped_row}) # Merge updates

        else: # New row added
             mapped_row['id'] = get_next_id(chapters_table) # Assign new ID
             mapped_row['previous_word_count'] = 0
             mapped_row['last_edited'] = get_local_now().isoformat()
             mapped_row['_changed'] = True # Mark as changed for timestamp
             current_app_state.append(mapped_row)
             needs_save = True

    # Check for deleted rows
    original_ids = {c['id'] for c in st.session_state.app_data['chapters']}
    edited_ids = {row['_id'] for row in edited_chapters_list if row['_id'] is not None}
    if original_ids != edited_ids:
        needs_save = True
        # The save function handles removal based on IDs present in the final list

    # Perform save if changes detected
    if needs_save:
        st.session_state.app_data['chapters'] = current_app_state
        save_data(st.session_state.app_data)
        st.toast("Changes saved automatically!", icon="💾")
        # Use rerun cautiously, might interrupt user editing flow if too frequent
        # Consider triggering rerun only on row additions/deletions?
        # For now, rely on Streamlit's natural rerun on widget interactions.
        # We need to rerun to update calculated fields like countdown/delta
        st.rerun()


# --- Tab 2: Editing Pass Board ---
with tab2:
    st.header("Editing Pass Focus")

    all_passes = st.session_state.app_data.get('editing_passes', [])
    chapter_map = {ch['id']: ch['title'] for ch in st.session_state.app_data.get('chapters', [])}
    chapter_options = {0: "None"} # 0 or None represents no specific chapter
    chapter_options.update({ch['id']: f"Ch {i+1}: {ch['title']}" for i, ch in enumerate(st.session_state.app_data.get('chapters', []))})


    # Group passes by focus area
    passes_by_focus = {}
    for p in all_passes:
        focus = p.get('focus_area', 'Uncategorized')
        if focus not in passes_by_focus:
            passes_by_focus[focus] = []
        passes_by_focus[focus].append(p)

    # Display passes using expanders for groups
    for focus_area, passes in passes_by_focus.items():
        with st.expander(f"**{focus_area}** ({len(passes)} items)", expanded=True):
            for p in sorted(passes, key=lambda x: x.get('id')):
                 pass_id = p['id']
                 col1, col2, col3 = st.columns([0.1, 0.8, 0.1])
                 with col1:
                     new_completed_status = st.checkbox("", value=p.get('completed', False), key=f"pass_cb_{pass_id}")
                     if new_completed_status != p.get('completed', False):
                         p['completed'] = new_completed_status
                         save_data(st.session_state.app_data) # Autosave on change
                         st.rerun() # Rerun to reflect change immediately

                 with col2:
                    chapter_title = chapter_map.get(p.get('chapter_id'))
                    link_text = f" (Ch: {chapter_title})" if chapter_title else ""
                    display_text = f"~~{p['description']}~~" if p.get('completed') else p['description']
                    st.markdown(f"{display_text}{link_text}", unsafe_allow_html=True)

                 with col3:
                     if st.button("🗑️", key=f"del_pass_{pass_id}", help="Delete this pass"):
                        st.session_state.app_data['editing_passes'] = [item for item in st.session_state.app_data['editing_passes'] if item['id'] != pass_id]
                        save_data(st.session_state.app_data)
                        st.rerun()


    st.divider()
    # Form to add a new editing pass
    with st.form("new_pass_form", clear_on_submit=True):
        st.subheader("Add New Editing Pass")
        new_focus = st.text_input("Focus Area (e.g., Pacing, Character Voice)")
        new_desc = st.text_area("Description (Markdown enabled)")
        # Allow linking to a chapter (optional)
        new_chapter_id_display = st.selectbox("Link to Chapter (Optional)", options=list(chapter_options.values()), key="new_pass_chapter_sel")
        # Map display name back to ID (0 means None)
        new_chapter_id = next((id for id, display in chapter_options.items() if display == new_chapter_id_display), None)


        submitted = st.form_submit_button("Add Pass")
        if submitted and new_focus and new_desc:
            new_pass_id = get_next_id(editing_passes_table)
            new_pass = {
                'id': new_pass_id,
                'focus_area': new_focus,
                'description': new_desc,
                'chapter_id': new_chapter_id if new_chapter_id != 0 else None,
                'completed': False
            }
            if 'editing_passes' not in st.session_state.app_data:
                 st.session_state.app_data['editing_passes'] = []
            st.session_state.app_data['editing_passes'].append(new_pass)
            save_data(st.session_state.app_data)
            st.toast("Editing pass added!", icon="✨")
            st.rerun() # Rerun to show the new pass
        elif submitted:
            st.warning("Please provide both a Focus Area and Description.")


# --- Tab 3: To-Do List ---
with tab3:
    st.header("General To-Do List")

    all_todos = st.session_state.app_data.get('todos', [])

    # Display To-Dos
    if not all_todos:
        st.markdown("_Nothing here yet. Add some tasks below!_")

    for todo in sorted(all_todos, key=lambda x: x.get('id')):
         todo_id = todo['id']
         col1, col2, col3 = st.columns([0.1, 0.8, 0.1])

         with col1:
             new_completed_status = st.checkbox("", value=todo.get('completed', False), key=f"todo_cb_{todo_id}")
             if new_completed_status != todo.get('completed', False):
                 todo['completed'] = new_completed_status
                 save_data(st.session_state.app_data) # Autosave on change
                 st.rerun() # Rerun to reflect change visually

         with col2:
             display_text = f"~~{todo['task']}~~" if todo.get('completed') else todo['task']
             st.markdown(display_text, unsafe_allow_html=True) # Allows strikethrough

         with col3:
              if st.button("🗑️", key=f"del_todo_{todo_id}", help="Delete this task"):
                  st.session_state.app_data['todos'] = [item for item in st.session_state.app_data['todos'] if item['id'] != todo_id]
                  save_data(st.session_state.app_data)
                  st.rerun()

    st.divider()
    # Input for adding new To-Dos
    new_task_text = st.text_input("Add a new To-Do item:", key="new_todo_input", placeholder="e.g., Final read-through for typos")
    if st.button("Add Task", key="add_todo_btn"):
        if new_task_text:
            new_todo_id = get_next_id(todos_table)
            new_todo = {'id': new_todo_id, 'task': new_task_text, 'completed': False}
            if 'todos' not in st.session_state.app_data:
                 st.session_state.app_data['todos'] = []
            st.session_state.app_data['todos'].append(new_todo)
            save_data(st.session_state.app_data)
            st.toast("To-Do item added!", icon="👍")
            # Clear the input field by rerunning (or using form's clear_on_submit if inside a form)
            st.rerun()
        else:
            st.warning("Task cannot be empty.")


# --- Autosave Check (General State Comparison) ---
# A secondary check in case specific handlers missed something.
# Compare current state hash with last saved hash.
# current_state_hash = json.dumps(st.session_state.app_data, sort_keys=True, default=str)
# if current_state_hash != st.session_state.get('last_saved_state', ''):
#     print("DEBUG: General state change detected, saving...")
#     save_data(st.session_state.app_data)
#     st.session_state.last_saved_state = current_state_hash
#     # Avoid rerun here unless absolutely necessary to prevent loops