import ast
import json
import os


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET_DIRECTORY = os.path.join(PROJECT_ROOT, "antrobot_ros")
OUTPUT_DIRECTORY = os.path.join(PROJECT_ROOT, "artifacts", "part_1")
OUTPUT_FILE = os.path.join(OUTPUT_DIRECTORY, "function_chunks.json")
STATISTICS_FILE = os.path.join(OUTPUT_DIRECTORY, "statistics.json")
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

def get_function_chunks(file_path):
    with open(file_path, "r") as source_file:
        source_code = source_file.read()
    
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
                "file_path": os.path.relpath(file_path, PROJECT_ROOT),
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

def find_python_files(directory):
    python_files = []
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith(".py"):
                python_files.append(os.path.join(root, file))
    return sorted(python_files)


def save_chunks(chunks, output_file=OUTPUT_FILE):
    output_directory = os.path.dirname(output_file)
    os.makedirs(output_directory, exist_ok=True)

    with open(output_file, "w", encoding="utf-8") as artifact_file:
        json.dump(chunks, artifact_file, indent=2, ensure_ascii=False)


def save_statistics(chunks, processed_files, output_file=STATISTICS_FILE):
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

    output_directory = os.path.dirname(output_file)
    os.makedirs(output_directory, exist_ok=True)

    with open(output_file, "w", encoding="utf-8") as artifact_file:
        json.dump(statistics, artifact_file, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    # Dynamically discover Python files within the antrobot_ros directory
    #` Only process files directly under antrobot_ros/antrobot_ros`, not other sub-folders
    files_to_process = find_python_files(TARGET_DIRECTORY)
    processed_file_paths = [
        os.path.relpath(file_path, PROJECT_ROOT) for file_path in files_to_process
    ]
    
    all_chunks = []
    for file_path in files_to_process:
        if os.path.exists(file_path):
            try:
                all_chunks.extend(get_function_chunks(file_path))
            except Exception as e:
                print(f"Error processing {file_path}: {e}")

    retain_user_defined_function_calls(all_chunks)
    save_chunks(all_chunks)
    save_statistics(all_chunks, processed_file_paths)
    print(f"Saved {len(all_chunks)} chunks to {OUTPUT_FILE}")
    print(f"Saved chunk statistics to {STATISTICS_FILE}")
