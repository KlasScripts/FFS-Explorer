name     = "WhatsApp Poll Votes"
description = ("One row per selected option on a WhatsApp poll. The poll itself stays in WhatsApp Messages, with the question, the options, and a count of these rows. The voter names are here.")
app_path = "data/data/com.whatsapp"
files    = {
    "msgstore": "databases/msgstore.db",
    "wa":       "databases/wa.db",
}
optional_files = {
    "msgstore_wal": "databases/msgstore.db-wal",
    "msgstore_shm": "databases/msgstore.db-shm",
    "wa_wal": "databases/wa.db-wal",
    "wa_shm": "databases/wa.db-shm",
    "prefs_light": "shared_prefs/com.whatsapp_preferences_light.xml",
}

timestamp_fields = {"sent_time": "ms"}
core_fields = ["chat", "question", "option", "voter", "sent_time"]
hidden_fields = ["addon_id", "selected_id", "chat_id"]
record_source = [
    {
        "label":        "Vote",
        "file_key":     "msgstore",
        "table":        "message_add_on",
        "rowid_fields": ["addon_id"],
    },
    {
        "label":        "Selected option",
        "file_key":     "msgstore",
        "table":        "message_add_on_poll_vote_selected_option",
        "rowid_fields": ["selected_id"],
    },
    {
        "label":        "Poll message",
        "file_key":     "msgstore",
        "table":        "message",
        "rowid_fields": ["message_id"],
    },
]


def _messages():
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).with_name("whatsapp.py")
    key = "ffs_android_whatsapp"
    mod = sys.modules.get(key)
    if mod is None:
        spec = importlib.util.spec_from_file_location(key, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return mod


def run(paths):
    from artifact_runner import open_db_readonly
    wa = _messages()
    conn = open_db_readonly(paths["msgstore"])
    conn.execute("ATTACH DATABASE ? AS contacts_db", (f'file:{paths["wa"]}?mode=ro',))
    _, reg, username, self_lid = wa._owner(paths)
    if not username:
        username = wa._username_for_lid(conn, self_lid)
    rows = wa.poll_vote_records(conn, wa._owner_label(reg, username))
    conn.close()
    return rows
