import mimetypes
import os
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client
from tqdm import tqdm

BASE_DIR = Path(__file__).resolve().parent
IMAGES_DIR = BASE_DIR / "images"
DATASET_DIR = BASE_DIR / "dataset"
BUCKET = "captcha-images"

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def list_existing_files() -> set[str]:
    existing = set()
    offset = 0
    limit = 100

    while True:
        batch = (
            supabase.storage.from_(BUCKET)
            .list(path="", options={"limit": limit, "offset": offset, "sortBy": {"column": "name", "order": "asc"}})
        )
        if not batch:
            break

        for item in batch:
            name = item.get("name")
            if isinstance(name, str):
                existing.add(name)

        if len(batch) < limit:
            break
        offset += limit

    return existing


def iter_local_files() -> list[Path]:
    files = []
    if IMAGES_DIR.exists():
        files.extend(sorted(IMAGES_DIR.glob("*_inner.jpg")))
        files.extend(sorted(IMAGES_DIR.glob("*_outer.jpg")))
    if DATASET_DIR.exists():
        files.extend(sorted(DATASET_DIR.glob("*_label.json")))
    return [path for path in files if path.is_file()]


def upload_file(path: Path) -> None:
    content_type, _ = mimetypes.guess_type(path.name)
    if not content_type:
        content_type = "application/octet-stream"

    with path.open("rb") as file_obj:
        supabase.storage.from_(BUCKET).upload(
            path=path.name,
            file=file_obj,
            file_options={"content-type": content_type, "upsert": "false"},
        )


def main() -> None:
    local_files = iter_local_files()
    if not local_files:
        print("Nenhum arquivo local encontrado em images/ e dataset/")
        return

    existing = list_existing_files()
    uploaded = 0
    skipped = 0
    failed = 0

    for path in tqdm(local_files, desc="Upload Supabase", unit="file"):
        if path.name in existing:
            skipped += 1
            continue

        try:
            upload_file(path)
            uploaded += 1
        except Exception as exc:
            failed += 1
            print(f"Erro ao subir {path.name}: {exc}")

    print("\nResumo:")
    print(f"- Total local: {len(local_files)}")
    print(f"- Uploads: {uploaded}")
    print(f"- Pulados (já existiam): {skipped}")
    print(f"- Falhas: {failed}")


if __name__ == "__main__":
    main()
