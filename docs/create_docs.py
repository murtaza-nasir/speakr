import os
from pathlib import Path

def create_markdown_doc(base_dir):
    output = []
    
    # Add header
    output.append("# Project Files\n")
    output.append("Generated documentation of all project files.\n")

    # Function to read and format file content
    def add_file_content(filepath, relative_path):
        output.append(f"\n## {relative_path}\n")
        output.append("```" + get_file_extension(filepath) + "\n")
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                output.append(f.read())
        except Exception as e:
            output.append(f"Error reading file: {e}")
        output.append("```\n")

    def get_file_extension(filepath):
        ext = os.path.splitext(filepath)[1][1:].lower()
        # Map file extensions to markdown code block languages
        extension_map = {
            'py': 'python',
            'html': 'html',
            'js': 'javascript',
            'css': 'css',
            'sh': 'bash',
            'md': 'markdown',
            'txt': 'text'
        }
        return extension_map.get(ext, '')

    # List of important file patterns to include
    patterns = [
        '*.py',
        '*.html',
        '*.js',
        '*.css',
        '*.sh',
        'requirements.txt'
    ]

    # Walk through directory and add files
    for root, _, _ in os.walk(base_dir):
        for pattern in patterns:
            for filepath in Path(root).glob(pattern):
                if 'venv' not in str(filepath) and '__pycache__' not in str(filepath):
                    relative_path = os.path.relpath(filepath, base_dir)
                    add_file_content(filepath, relative_path)

    # Write to output file
    output_path = os.path.join(base_dir, 'project_files.md')
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output))
    
    return output_path

if __name__ == "__main__":
    # Get the current directory
    current_dir = os.getcwd()
    
    # Create the markdown file
    output_file = create_markdown_doc(current_dir)
    print(f"Created markdown documentation at: {output_file}")