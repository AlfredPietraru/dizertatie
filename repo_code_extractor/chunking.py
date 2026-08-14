import ast
import json
from pathlib import Path

CHUNK_LENGTH_BINS = (
    (0, 49),
    (50, 199),
    (200, 499),
    (500, 999),
    (1000, 1999),
    (2000, None),
)


class FunctionCallVisitor(ast.NodeVisitor):
    """Collect calls made by one function without entering nested functions."""

    def __init__(self, root_function):
        self.root_function = root_function
        self.called_function_names = set()

    def visit_FunctionDef(self, node):
        if node is self.root_function:
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        if node is self.root_function:
            self.generic_visit(node)

    def visit_Lambda(self, node):
        return

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name):
            self.called_function_names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            self.called_function_names.add(node.func.attr)

        self.generic_visit(node)


def get_called_function_names(function_node):
    visitor = FunctionCallVisitor(function_node)
    visitor.visit(function_node)
    return sorted(visitor.called_function_names)

def get_function_chunks(file_path: str | Path, project_root: str | Path | None = None):
    file_path = Path(file_path)
    project_root = Path(project_root).resolve() if project_root else file_path.parent
    source_code = file_path.read_text(encoding="utf-8")
    
    tree = ast.parse(source_code)
    
    chunks = []
    source_lines = source_code.splitlines(True) # Keep newlines to accurately reconstruct source

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start_line = node.lineno
            end_line = node.end_lineno # This should be available in modern Python versions (3.8+)

            if end_line is None: # Fallback for older versions or if end_lineno is somehow missing
                # Try to determine end_lineno by finding the last line of the function body
                if node.body:
                    last_body_item = node.body[-1]
                    end_line = last_body_item.end_lineno if hasattr(last_body_item, 'end_lineno') else last_body_item.lineno
                else:
                    end_line = start_line # If no body, function is a single line (e.g., pass)
            
            # Slice the source lines to get the function's code, correcting for 0-based indexing
            function_source = "".join(source_lines[start_line - 1:end_line]).strip()

            chunks.append({
                "function_name": node.name,
                "function_parameters": [arg.arg for arg in node.args.args],
                "file_path": file_path.resolve().relative_to(project_root).as_posix(),
                "function_lines": (start_line, end_line),
                "chunk": function_source,
                "called_function_names": get_called_function_names(node),
            })
    return chunks


def retain_user_defined_function_calls(chunks):
    user_defined_function_names = {chunk["function_name"] for chunk in chunks}

    for chunk in chunks:
        chunk["called_user_functions"] = [
            function_name
            for function_name in chunk.pop("called_function_names")
            if function_name in user_defined_function_names
        ]

def find_python_files(directory: str | Path) -> list[Path]:
    """Return Python files recursively and deterministically."""
    return sorted(Path(directory).resolve().rglob("*.py"))


def save_chunks(chunks, output_file: str | Path):
    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        json.dumps(chunks, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def save_statistics(chunks, processed_files, output_file: str | Path):
    distribution = {}
    chunks_per_file = {file_path: 0 for file_path in processed_files}

    for chunk in chunks:
        file_path = chunk["file_path"]
        chunks_per_file[file_path] = chunks_per_file.get(file_path, 0) + 1

    for minimum, maximum in CHUNK_LENGTH_BINS:
        label = f"{minimum}+ characters" if maximum is None else f"{minimum}-{maximum} characters"
        distribution[label] = sum(
            1
            for chunk in chunks
            if len(chunk["chunk"]) >= minimum
            and (maximum is None or len(chunk["chunk"]) <= maximum)
        )

    statistics = {
        "total_chunks": len(chunks),
        "total_files_processed": len(processed_files),
        "files_processed": [
            {
                "file_path": file_path,
                "chunks_extracted": chunks_per_file[file_path],
            }
            for file_path in processed_files
        ],
        "chunk_length_unit": "characters",
        "chunk_length_distribution": distribution,
    }

    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        json.dumps(statistics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def extract_function_chunks(
    source_directory: str | Path,
    *,
    project_root: str | Path | None = None,
) -> list[dict]:
    """Extract function-level source chunks from a Python source tree."""
    source_directory = Path(source_directory).resolve()
    if not source_directory.is_dir():
        raise NotADirectoryError(f"Source directory does not exist: {source_directory}")
    root = Path(project_root).resolve() if project_root else source_directory
    files = find_python_files(source_directory)
    chunks: list[dict] = []
    for file_path in files:
        chunks.extend(get_function_chunks(file_path, root))
    retain_user_defined_function_calls(chunks)
    return chunks


def extract_function_chunks_from_roots(
    source_directories: list[str | Path],
    *,
    project_root: str | Path,
) -> list[dict]:
    """Extract chunks from several selected roots with one call relationship set."""
    root = Path(project_root).resolve()
    chunks: list[dict] = []
    for directory in source_directories:
        source_directory = Path(directory).resolve()
        if not source_directory.is_dir():
            continue
        for file_path in find_python_files(source_directory):
            chunks.extend(get_function_chunks(file_path, root))
    retain_user_defined_function_calls(chunks)
    return chunks


def write_chunk_artifacts(
    source_directory: str | Path,
    output_directory: str | Path,
    *,
    project_root: str | Path | None = None,
) -> tuple[Path, Path]:
    """Extract chunks and write both chunks and statistics JSON artifacts."""
    source_directory = Path(source_directory).resolve()
    output_directory = Path(output_directory).resolve()
    root = Path(project_root).resolve() if project_root else source_directory
    files = find_python_files(source_directory)
    chunks = extract_function_chunks(source_directory, project_root=root)
    processed = [path.relative_to(root).as_posix() for path in files]
    chunks_path = output_directory / "function_chunks.json"
    statistics_path = output_directory / "statistics.json"
    save_chunks(chunks, chunks_path)
    save_statistics(chunks, processed, statistics_path)
    return chunks_path, statistics_path


def write_chunk_artifacts_from_roots(
    source_directories: list[str | Path],
    output_directory: str | Path,
    *,
    project_root: str | Path,
) -> tuple[Path, Path]:
    """Write chunk artifacts for several explicitly selected source roots."""
    root = Path(project_root).resolve()
    output_directory = Path(output_directory).resolve()
    files = [
        file_path
        for directory in source_directories
        if Path(directory).resolve().is_dir()
        for file_path in find_python_files(directory)
    ]
    chunks = extract_function_chunks_from_roots(
        source_directories, project_root=root
    )
    processed = [path.relative_to(root).as_posix() for path in sorted(files)]
    chunks_path = output_directory / "function_chunks.json"
    statistics_path = output_directory / "statistics.json"
    save_chunks(chunks, chunks_path)
    save_statistics(chunks, processed, statistics_path)
    return chunks_path, statistics_path

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Extract Python functions into JSON chunks.")
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=Path("artifacts/function_chunks"))
    args = parser.parse_args()
    chunks_path, statistics_path = write_chunk_artifacts(args.source, args.output)
    print(f"Saved chunks to {chunks_path}")
    print(f"Saved chunk statistics to {statistics_path}")
