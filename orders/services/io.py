# imports from std lib
from pathlib import Path



# writes to a file
# @params:
#   path_to_file - path to write to target file
#   text_to_write - content to write in target file
#   bytes_mode - ? write as bytes : write as text
def write_file(path_to_file, text_to_write, bytes_mode = False):
    p = path_to_file
    p.parent.mkdir(parents=True, exist_ok=True)
    if bytes_mode:
        p.write_bytes(text_to_write)
    else:
        p.write_text(text_to_write)

# finds next unused number for file names from current day
# @params:
#   date - current date
# @returns:
#   next unused ID number
def next_id_scan(d: str) -> int:
    raw_dir = Path(f"order_info/{d}/details/raw_bytes")
    raw_dir.mkdir(parents=True, exist_ok=True)

    ids = []
    for p in raw_dir.glob("*.bin"):
        stem = p.stem  # e.g., "07_Maria-Gonzalez"
        # grab the prefix before the first underscore
        prefix = stem.split("_", 1)[0]
        if prefix.isdigit():
            ids.append(int(prefix))
    return (max(ids) + 1) if ids else 1

# formats next_id_scan result into string of length 5
# @params:
#   date - current date
# @returns:
#   id_str - formatted ID string
def next_id_scan_str(date) -> str:
    id_str = f"{next_id_scan(date):02d}"
    return id_str
