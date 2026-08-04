import json
import os
from pathlib import Path

import chromadb
from chromadb.errors import NotFoundError
from dotenv import load_dotenv
from huggingface_hub import InferenceClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS_DIRECTORY = PROJECT_ROOT / "artifacts" / "part_1"
CHUNKS_FILE = ARTIFACTS_DIRECTORY / "function_chunks.json"
CHROMA_DB_DIRECTORY = ARTIFACTS_DIRECTORY / "chroma_db"
COLLECTION_NAME = "antrobot_function_chunks"
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
BATCH_SIZE = 32

load_dotenv(PROJECT_ROOT / ".env")


def chroma_database_exists():
    return CHROMA_DB_DIRECTORY.exists() and any(CHROMA_DB_DIRECTORY.iterdir())


def read_chunks(chunks_file):
    with open(chunks_file, "r", encoding="utf-8") as source_file:
        return json.load(source_file)


def create_chunk_id(chunk, index):
    file_path = chunk["file_path"].replace("/", "__")
    function_name = chunk["function_name"]
    start_line, end_line = chunk["function_lines"]
    return f"{index:04d}_{file_path}_{function_name}_{start_line}_{end_line}"


def normalize_metadata_value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    return json.dumps(value, ensure_ascii=False)


def create_metadata(chunk):
    return {
        key: normalize_metadata_value(value)
        for key, value in chunk.items()
        if key != "chunk"
    }


def create_embedding_client():
    return InferenceClient(
        provider="hf-inference",
        api_key=os.environ["HF_TOKEN"],
    )


def embed_texts(client, texts):
    return client.feature_extraction(
        texts,
        model=EMBEDDING_MODEL,
        normalize=True,
    )


def recreate_collection(chroma_client):
    try:
        chroma_client.delete_collection(COLLECTION_NAME)
    except (ValueError, NotFoundError):
        pass

    return chroma_client.create_collection(
        name=COLLECTION_NAME,
        metadata={
            "embedding_model": EMBEDDING_MODEL,
            "source_file": str(CHUNKS_FILE.relative_to(PROJECT_ROOT)),
        },
    )


def ingest_chunks():
    if chroma_database_exists():
        print(f"ChromaDB already exists at {CHROMA_DB_DIRECTORY}")
        print("Skipping embedding pipeline.")
        print("Delete this folder manually to rebuild the database.")
        return

    chunks = read_chunks(CHUNKS_FILE)
    embedding_client = create_embedding_client()
    chroma_client = chromadb.PersistentClient(path=str(CHROMA_DB_DIRECTORY))
    collection = recreate_collection(chroma_client)

    for batch_start in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[batch_start:batch_start + BATCH_SIZE]
        documents = [chunk["chunk"] for chunk in batch]
        embeddings = embed_texts(embedding_client, documents)
        ids = [
            create_chunk_id(chunk, batch_start + offset)
            for offset, chunk in enumerate(batch)
        ]
        metadatas = [create_metadata(chunk) for chunk in batch]

        collection.add(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas, # type: ignore
        )

        print(f"Stored {batch_start + len(batch)}/{len(chunks)} chunks")

    print(f"ChromaDB saved to {CHROMA_DB_DIRECTORY}")
    print(f"Collection: {COLLECTION_NAME}")
    print(f"Total chunks stored: {collection.count()}")


if __name__ == "__main__":
    ingest_chunks()
