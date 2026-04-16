import base64
import os
from urllib.parse import urljoin


def resolve_polling_url(api_base_url: str, location: str) -> str:
    """Resolve a Location header value to an absolute polling URL."""
    loc = location.strip()
    if loc.lower().startswith(("http://", "https://")):
        return loc
    base = api_base_url.rstrip("/") + "/"
    return urljoin(base, loc)


def file_to_base64(file_path: str) -> tuple[str, str]:
    """Read a file and return its base64-encoded content and file extension."""
    _, extension = os.path.splitext(file_path)
    with open(file_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")
    return encoded, extension


def _unique_path(directory_path: str, file_name: str) -> str:
    """Return a path that doesn't conflict with existing files by appending (1), (2), etc."""
    base, ext = os.path.splitext(file_name)
    candidate = os.path.join(directory_path, file_name)
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory_path, f"{base} ({counter}){ext}")
        counter += 1
    return candidate


def write_file_from_base64(base64_data: str, directory_path: str, file_name: str) -> None:
    """Decode base64 data and write it to a file at the given directory with the given name."""
    os.makedirs(directory_path, exist_ok=True)
    output_path = _unique_path(directory_path, file_name)
    with open(output_path, "wb") as f:
        f.write(base64.b64decode(base64_data))


def write_file_from_bytes(data: bytes, directory_path: str, file_name: str) -> str:
    """Write raw bytes to a file at the given directory with the given name."""
    os.makedirs(directory_path, exist_ok=True)
    output_path = _unique_path(directory_path, file_name)
    with open(output_path, "wb") as f:
        f.write(data)
    return output_path
