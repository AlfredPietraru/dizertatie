import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS_DIRECTORY = PROJECT_ROOT / "artifacts" / "part_1"
CHUNKS_FILE = ARTIFACTS_DIRECTORY / "function_chunks.json"
CHROMA_DB_DIRECTORY = ARTIFACTS_DIRECTORY / "chroma_db"
COLLECTION_NAME = "antrobot_function_chunks"
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
BATCH_SIZE = 32

def chroma_database_exists(directory: str | Path = CHROMA_DB_DIRECTORY):
    directory = Path(directory)
    return directory.exists() and any(directory.iterdir())


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
    try:
        from huggingface_hub import InferenceClient
    except ImportError as error:
        raise ImportError("Vector ingestion requires huggingface-hub") from error
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


def recreate_collection(chroma_client, collection_name=COLLECTION_NAME, source_file=CHUNKS_FILE):
    try:
        from chromadb.errors import NotFoundError
    except ImportError as error:
        raise ImportError("Vector ingestion requires chromadb") from error
    try:
        chroma_client.delete_collection(collection_name)
    except (ValueError, NotFoundError):
        pass

    return chroma_client.create_collection(
        name=collection_name,
        metadata={
            "embedding_model": EMBEDDING_MODEL,
            "source_file": str(source_file),
        },
    )


def ingest_chunks(
    chunks_file: str | Path = CHUNKS_FILE,
    database_directory: str | Path = CHROMA_DB_DIRECTORY,
    collection_name: str = COLLECTION_NAME,
    *,
    recreate: bool = False,
):
    """Embed function chunks into a persistent ChromaDB collection."""
    try:
        import chromadb
    except ImportError as error:
        raise ImportError("Vector ingestion requires chromadb") from error
    try:
        from dotenv import load_dotenv
    except ImportError as error:
        raise ImportError("Vector ingestion requires python-dotenv") from error

    load_dotenv(PROJECT_ROOT / ".env")
    chunks_file = Path(chunks_file).resolve()
    database_directory = Path(database_directory).resolve()
    if chroma_database_exists(database_directory) and not recreate:
        print(f"ChromaDB already exists at {database_directory}")
        print("Skipping embedding pipeline.")
        print("Delete this folder manually to rebuild the database.")
        return None

    chunks = read_chunks(chunks_file)
    embedding_client = create_embedding_client()
    chroma_client = chromadb.PersistentClient(path=str(database_directory))
    collection = recreate_collection(chroma_client, collection_name, chunks_file)

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

    print(f"ChromaDB saved to {database_directory}")
    print(f"Collection: {collection_name}")
    print(f"Total chunks stored: {collection.count()}")
    return collection


if __name__ == "__main__":
    ingest_chunks()
