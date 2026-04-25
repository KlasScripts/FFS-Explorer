# Add Android artifact parsers in this folder.
# Each file must define:
#   name         = "Human Readable Name"
#   target_paths = ["path/relative/to/user/partition/file.db"]
#   def run(db): ...   # db is a sqlite3.Connection; return list of dicts
